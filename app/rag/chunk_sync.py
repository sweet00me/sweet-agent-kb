"""
文件：app/rag/chunk_sync.py

用途：为 Storage 已解析的 MD/TXT/DOCX 建立本地增量 Chunk 缓存。

规则：
- --sync 才允许落盘；默认只读预览。
- 只以 document_sync.sqlite3 中的成功解析记录为输入。
- 每次校验源文件 SHA256，避免旧解析结果进入后续索引。
- 内容寻址、原子写入新的 Chunk JSON；旧缓存不自动删除。
- 修改分块参数或解析器版本时自动产生新版本。
- 不调用 Embedding/DeepSeek、不访问或修改 ChromaDB。
- 不自动处理文件删除对应的旧向量，下一阶段另设索引状态门禁。
"""

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.rag.document_chunking import (
    DEFAULT_MAX_CHARS,
    DEFAULT_OVERLAP_CHARS,
    build_document_chunks,
)

STORAGE_ROOT = Path('/home/sweet/Storage')
KB_ROOT = STORAGE_ROOT / 'KnowledgeBase'
PARSED_DIR = KB_ROOT / 'parsed_documents'
SOURCE_DB = KB_ROOT / 'sqlite' / 'document_sync.sqlite3'
CHUNK_DIR = KB_ROOT / 'chunk_cache'
CHUNK_DB = KB_ROOT / 'sqlite' / 'chunk_sync.sqlite3'
CHUNKER_VERSION = 'document_chunking_v1'

CREATE_STATE_SQL = '''
CREATE TABLE IF NOT EXISTS chunk_cache (
    source_path TEXT PRIMARY KEY,
    source_sha256 TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    chunker_version TEXT NOT NULL,
    document_id TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    review_count INTEGER NOT NULL,
    cache_path TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
'''


def sha256_file(path: Path) -> str:
    """按块计算源文件 SHA256；不读取全部文件到内存。"""
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _read_parsed_rows(db_path: Path) -> list[dict]:
    if not db_path.is_file():
        raise FileNotFoundError(f'解析状态数据库不存在：{db_path}')
    with sqlite3.connect(f'{db_path.as_uri()}?mode=ro', uri=True) as conn:
        rows = conn.execute('''
            SELECT source_path, sha256, parser_version, parsed_path
            FROM parsed_documents ORDER BY source_path
        ''').fetchall()
    return [dict(zip(('source', 'sha256', 'parser_version', 'parsed_path'), row))
            for row in rows]


def _read_chunk_state(db_path: Path) -> dict[str, dict]:
    if not db_path.is_file():
        return {}
    with sqlite3.connect(f'{db_path.as_uri()}?mode=ro', uri=True) as conn:
        rows = conn.execute('''
            SELECT source_path, source_sha256, parser_version, chunker_version,
                   document_id, chunk_count, review_count, cache_path
            FROM chunk_cache
        ''').fetchall()
    keys = ('source', 'sha256', 'parser_version', 'chunker_version',
            'document_id', 'chunk_count', 'review_count', 'cache_path')
    return {row[0]: dict(zip(keys, row)) for row in rows}


def _checked_source(root: Path, source: str) -> Path | None:
    # 从 SQLite 读取的路径也不能直接拼接后访问任意位置。
    if not isinstance(source, str) or '\\' in source or not source:
        raise ValueError('来源路径格式无效')
    parts = source.split('/')
    if any(not p or p in ('.', '..') or p.startswith('.') for p in parts):
        raise ValueError('来源路径含有禁止的部分')
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError('来源路径中存在符号链接')
    if not path.exists():
        return None
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError('来源不是普通文件或不在 Storage 内')
    return resolved


def _checked_parsed_path(path: str, parsed_root: Path) -> Path:
    target = Path(path).resolve(strict=True)
    if not target.is_relative_to(parsed_root) or not target.is_file():
        raise ValueError('解析缓存路径不合法')
    return target


def _cache_key(source: str, sha: str, parser_version: str, chunker_version: str) -> str:
    identity = '\0'.join([source, sha, parser_version, chunker_version])
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()


def _atomic_json(path: Path, content: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.tmp-', suffix='.json', delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(content, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # 尽可能同步目录项，确保异常停机后的持久化。
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sync_chunks(*, source_root: Path, parsed_root: Path, source_db: Path,
                cache_dir: Path, chunk_db: Path, write: bool = False,
                max_chars: int = DEFAULT_MAX_CHARS,
                overlap_chars: int = DEFAULT_OVERLAP_CHARS) -> dict:
    """安全增量同步；所有路径参数显式传入，便于隔离测试。"""
    source_root = Path(source_root).resolve(strict=True)
    parsed_root = Path(parsed_root).resolve(strict=True)
    cache_dir = Path(cache_dir).resolve(strict=False)
    chunk_db = Path(chunk_db)
    chunker = f'{CHUNKER_VERSION}:max={max_chars}:overlap={overlap_chars}'

    parsed_rows = _read_parsed_rows(Path(source_db))
    old = _read_chunk_state(chunk_db)

    summary = dict(documents=len(parsed_rows), new=0, modified=0, unchanged=0,
                   missing=0, pending_parse=0, failed=0, chunk_total=0,
                   chunks_requiring_review=0, preview_only=not write, errors=[])

    db = None
    try:
        if write:
            os.umask(0o077)
            chunk_db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            db = sqlite3.connect(chunk_db)
            db.execute(CREATE_STATE_SQL)
            db.commit()

        for row in parsed_rows:
            source = row['source']
            try:
                original = _checked_source(source_root, source)
                if original is None:
                    summary['missing'] += 1
                    continue

                if sha256_file(original) != row['sha256']:
                    # 源文件已修改但尚未重新解析；禁止把过期解析转成 Chunk。
                    summary['pending_parse'] += 1
                    continue

                parsed_path = _checked_parsed_path(row['parsed_path'], parsed_root)
                record = json.loads(parsed_path.read_text(encoding='utf-8'))
                if (record.get('source') != source or
                        record.get('sha256') != row['sha256'] or
                        not isinstance(record.get('document_id'), str)):
                    raise ValueError('解析 JSON 与 SQLite 中的来源或哈希不一致')
                config = _cache_key(source, row['sha256'], row['parser_version'], chunker)
                target = cache_dir / f'{config}.json'
                previous = old.get(source)
                same = (previous is not None and
                        previous['sha256'] == row['sha256'] and
                        previous['parser_version'] == row['parser_version'] and
                        previous['chunker_version'] == chunker and
                        previous['cache_path'] == str(target) and
                        target.is_file())

                if same:
                    # 读取并验证实际缓存，不能仅凭文件存在就跳过。
                    cached = json.loads(target.read_text(encoding='utf-8'))
                    if (cached.get('source') == source and
                            cached.get('sha256') == row['sha256'] and
                            cached.get('chunker_version') == chunker and
                            cached.get('document_id') == record['document_id'] and
                            isinstance(cached.get('chunks'), list) and
                            len(cached['chunks']) == previous['chunk_count']):
                        summary['unchanged'] += 1
                        summary['chunk_total'] += previous['chunk_count']
                        summary['chunks_requiring_review'] += previous['review_count']
                        continue

                chunks = build_document_chunks(record, max_chars=max_chars,
                                               overlap_chars=overlap_chars)
                if sha256_file(original) != row['sha256']:
                    summary['pending_parse'] += 1
                    continue

                status = 'new' if previous is None else 'modified'
                review_count = sum(1 for chunk in chunks if chunk['review_required'])
                if write:
                    payload = {
                        'source': source,
                        'sha256': row['sha256'],
                        'parser_version': row['parser_version'],
                        'chunker_version': chunker,
                        'document_id': record['document_id'],
                        'chunk_count': len(chunks),
                        'review_count': review_count,
                        'chunks': chunks,
                    }
                    if target.is_file():
                        cached = json.loads(target.read_text(encoding='utf-8'))
                        if cached != payload:
                            raise RuntimeError('内容寻址 Chunk 缓存已存在但内容不同')
                    else:
                        _atomic_json(target, payload)
                    db.execute('''
                        INSERT INTO chunk_cache
                        (source_path, source_sha256, parser_version, chunker_version,
                         document_id, chunk_count, review_count, cache_path, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_path) DO UPDATE SET
                            source_sha256=excluded.source_sha256,
                            parser_version=excluded.parser_version,
                            chunker_version=excluded.chunker_version,
                            document_id=excluded.document_id,
                            chunk_count=excluded.chunk_count,
                            review_count=excluded.review_count,
                            cache_path=excluded.cache_path,
                            updated_at=excluded.updated_at
                    ''', (source, row['sha256'], row['parser_version'], chunker,
                          record['document_id'], len(chunks), review_count, str(target),
                          datetime.now(timezone.utc).isoformat()))
                    db.commit()
                summary[status] += 1
                summary['chunk_total'] += len(chunks)
                summary['chunks_requiring_review'] += review_count
            except Exception as exc:
                summary['failed'] += 1
                summary['errors'].append({'source': source,
                                          'reason': f'{type(exc).__name__}: {exc}'})
        # source DB may retain records for files that have been excluded/deleted.
        # Under no circumstances silently delete their previous chunk cache entries.
        return summary
    finally:
        if db is not None:
            db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description='Storage 增量 Chunk 缓存；默认只读预览')
    parser.add_argument('--sync', action='store_true', help='写入本地 Chunk JSON 和状态 SQLite')
    parser.add_argument('--max-chars', type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument('--overlap-chars', type=int, default=DEFAULT_OVERLAP_CHARS)
    args = parser.parse_args()
    if not os.path.ismount(STORAGE_ROOT):
        raise SystemExit('Storage 尚未挂载，停止运行')
    from app.rag.sync_lock import cli_writer_lock
    with cli_writer_lock(STORAGE_ROOT, write=args.sync):
        result = sync_chunks(source_root=STORAGE_ROOT, parsed_root=PARSED_DIR,
                             source_db=SOURCE_DB, cache_dir=CHUNK_DIR,
                             chunk_db=CHUNK_DB, write=args.sync,
                             max_chars=args.max_chars, overlap_chars=args.overlap_chars)
    print('模式：', '本地 Chunk 缓存' if args.sync else '只读预览')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result['failed'] or result['pending_parse']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
