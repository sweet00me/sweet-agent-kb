"""文件：app/rag/vector_sync.py

12.6 Storage 向量增量索引（MD/TXT/DOCX）。
- 默认只读预览，不使用网络、不创建数据库。
- 必须显式 --write 才调用百炼 Embedding API 并修改独立 Chroma Collection。
- 严格排除用户指定的单个源文件，即使其已有解析/Chunk 缓存也不读取正文。
- 文档源文件 SHA256 与 Chunk 缓存版本双重校验；质量警告阻止上传。
- 每篇文档先 staged(inactive)，验证完成后激活新版本并清理旧 Chunk。
- 文件缺失、未完成解析或未在本次扫描发现：不执行删除。
- 该 Collection 尚未接入 Agent；未来检索务必过滤 is_active=True。
"""

import argparse
import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.rag.chunk_sync import _checked_source, sha256_file

STORAGE_ROOT = Path('/home/sweet/Storage')
KB_ROOT = STORAGE_ROOT / 'KnowledgeBase'
CHUNK_DB = KB_ROOT / 'sqlite' / 'chunk_sync.sqlite3'
CHUNK_DIR = KB_ROOT / 'chunk_cache'
VECTOR_DB = KB_ROOT / 'sqlite' / 'vector_sync.sqlite3'
CHROMA_PATH = KB_ROOT / 'chroma'
COLLECTION_NAME = 'sweet_kb_storage_v1'
SCHEMA = 'sweet_storage_vector_v1'
BATCH_SIZE = 8

# 精确排除：只排除这一份 Markdown，不排除 Diary 的其他文档。
EXCLUDED_SOURCES = frozenset({
    'KnowledgeBase/documents/Diary/24.7.16.md',
})

CREATE_SQL = '''
CREATE TABLE IF NOT EXISTS vector_documents (
    source_path TEXT PRIMARY KEY,
    source_sha256 TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    chunker_version TEXT NOT NULL,
    document_id TEXT NOT NULL,
    chunk_ids_json TEXT NOT NULL,
    indexed_at TEXT NOT NULL
)
'''


def _read_rows(path: Path) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f'Chunk 状态数据库不存在：{path}')
    with sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True) as db:
        records = db.execute('''
            SELECT source_path, source_sha256, parser_version, chunker_version,
                   document_id, chunk_count, review_count, cache_path
            FROM chunk_cache ORDER BY source_path
        ''').fetchall()
    keys = ('source', 'sha256', 'parser_version', 'chunker_version',
            'document_id', 'chunk_count', 'review_count', 'cache_path')
    return [dict(zip(keys, row)) for row in records]


def _read_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    with sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True) as db:
        rows = db.execute('''
            SELECT source_path, source_sha256, parser_version, chunker_version,
                   document_id, chunk_ids_json FROM vector_documents
        ''').fetchall()
    return {row[0]: {
        'sha256': row[1], 'parser_version': row[2],
        'chunker_version': row[3], 'document_id': row[4],
        'chunk_ids': json.loads(row[5]),
    } for row in rows}


def _load_verified(row: dict, root: Path, cache_dir: Path) -> dict:
    """检查原件、缓存路径及内容；禁止使用过期/来源不符的 Chunk。"""
    original = _checked_source(root, row['source'])
    if original is None:
        raise FileNotFoundError('原始文档已缺失')
    if sha256_file(original) != row['sha256']:
        raise ValueError('源文件已经修改，请先运行 document_sync --parse 和 chunk_sync --sync')

    cache_dir = Path(cache_dir).resolve(strict=True)
    expected = Path(row['cache_path'])
    actual = expected.resolve(strict=True)
    if (actual.parent != cache_dir or actual.suffix != '.json'
            or expected.is_symlink() or not actual.is_file()):
        raise ValueError('Chunk 缓存路径非法')
    payload = json.loads(actual.read_text(encoding='utf-8'))
    for key, expected_value in (
        ('source', row['source']), ('sha256', row['sha256']),
        ('parser_version', row['parser_version']),
        ('chunker_version', row['chunker_version']),
        ('document_id', row['document_id']),
        ('chunk_count', row['chunk_count']),
    ):
        if payload.get(key) != expected_value:
            raise ValueError(f'Chunk 缓存元数据不匹配：{key}')

    chunks = payload.get('chunks')
    if not isinstance(chunks, list) or len(chunks) != row['chunk_count'] or not chunks:
        raise ValueError('Chunk 数量不合法')
    ids = []
    review = 0
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict) or not isinstance(chunk.get('content'), str):
            raise ValueError('Chunk 数据结构错误')
        if (chunk.get('source') != row['source']
                or chunk.get('file_sha256') != row['sha256']
                or chunk.get('document_id') != row['document_id']
                or chunk.get('chunk_index') != index
                or not chunk['content'].strip()
                or len(chunk['content']) > 10000
                or 'data:image/' in chunk['content']):
            raise ValueError('Chunk 来源、顺序、长度或内容不合法')
        chunk_id = chunk.get('id')
        recomputed = hashlib.sha256(
            f"{row['document_id']}\0{index}\0{chunk['content']}".encode('utf-8')
        ).hexdigest()[:32]
        if chunk_id != recomputed:
            raise ValueError('Chunk ID 与内容哈希不一致')
        if chunk.get('content_chars') != len(chunk['content']):
            raise ValueError('Chunk 字符数与正文不一致')
        ids.append(chunk_id)
        review += bool(chunk.get('review_required') or chunk.get('warnings'))
    if len(set(ids)) != len(ids) or review != row['review_count']:
        raise ValueError('Chunk ID 或质量标志不一致')
    return {'row': row, 'payload': payload, 'chunks': chunks, 'ids': ids,
            'review': review, 'cache_digest': sha256_file(actual),
            'cache_path': actual}


def _is_same(old: dict | None, item: dict) -> bool:
    if old is None:
        return False
    row = item['row']
    return (old['sha256'] == row['sha256'] and
            old['parser_version'] == row['parser_version'] and
            old['chunker_version'] == row['chunker_version'] and
            old['document_id'] == row['document_id'] and
            old['chunk_ids'] == item['ids'])


def build_plan(*, source_root: Path, chunk_dir: Path, chunk_db: Path,
               vector_db: Path, source_filter: str | None = None) -> tuple[dict, list[dict]]:
    """完全只读：文件源与 Chunk 核验；不调用网络，不建立 SQLite/Chroma。"""
    source_root = Path(source_root).resolve(strict=True)
    rows = _read_rows(Path(chunk_db))
    existing = _read_state(Path(vector_db))
    if source_filter and source_filter in EXCLUDED_SOURCES:
        raise ValueError('该文件被向量化策略明确排除，不能使用 --source 绕过')
    if source_filter is not None and not any(r['source'] == source_filter for r in rows):
        raise ValueError('未找到指定文件的 Chunk 缓存记录')

    summary = dict(total=0, excluded=0, new=0, modified=0, unchanged=0,
                   failed=0, pending_parse=0, chunk_total=0,
                   chunks_requiring_review=0, preview_only=True, errors=[])
    plan = []
    for row in rows:
        source = row['source']
        if source in EXCLUDED_SOURCES:
            summary['excluded'] += 1
            continue
        if source_filter is not None and source != source_filter:
            continue
        summary['total'] += 1
        try:
            item = _load_verified(row, source_root, chunk_dir)
            old = existing.get(source)
            action = 'unchanged' if _is_same(old, item) else ('modified' if old else 'new')
            item['action'] = action
            summary[action] += 1
            summary['chunk_total'] += len(item['chunks'])
            summary['chunks_requiring_review'] += item['review']
            plan.append(item)
        except (FileNotFoundError, ValueError) as exc:
            summary['pending_parse'] += 1
            summary['errors'].append({'source': source, 'reason': str(exc)})
        except Exception as exc:
            summary['failed'] += 1
            summary['errors'].append({'source': source,
                                      'reason': f'{type(exc).__name__}: {exc}'})
    return summary, plan


def _collection_metadata(model: str, dimensions: int) -> dict:
    return {'embedding_model': model, 'embedding_dimensions': dimensions,
            'index_schema': SCHEMA}


def validate_collection(collection, model: str, dimensions: int) -> None:
    if collection.metadata != _collection_metadata(model, dimensions):
        raise RuntimeError('Chroma Collection 模型、维度或索引结构不一致')


def _chunk_metadata(item: dict, chunk: dict, active: bool) -> dict:
    row = item['row']
    return {
        'source': row['source'],
        'document_id': row['document_id'],
        'file_sha256': row['sha256'],
        'file_type': chunk['file_type'],
        'heading_path': chunk.get('heading_path') or '',
        'chunk_index': int(chunk['chunk_index']),
        'parser_version': row['parser_version'],
        'chunker_version': row['chunker_version'],
        'is_active': bool(active),
    }


def _index_one(collection, item: dict, embed_texts, dimensions: int,
               batch_size: int, vector_db: sqlite3.Connection,
               source_root: Path) -> int:
    """增量写入单篇文档。只有在向量全部就绪后才激活并提交状态。"""
    row = item['row']
    source = row['source']
    expected = set(item['ids'])
    existing = collection.get(where={'source': source}, include=['documents', 'metadatas'])
    known = {identifier: (content, metadata) for identifier, content, metadata in zip(
        existing['ids'], existing['documents'], existing['metadatas'])}

    missing = []
    for chunk in item['chunks']:
        chunk_id = chunk['id']
        if chunk_id in known:
            content, meta = known[chunk_id]
            if content != chunk['content'] or meta.get('source') != source:
                raise RuntimeError('已存在同 ID 但内容或来源冲突，拒绝覆盖')
        else:
            missing.append(chunk)

    embedded = 0
    for start in range(0, len(missing), batch_size):
        batch = missing[start:start + batch_size]
        texts = [x['content'] for x in batch]
        vectors = embed_texts(texts)
        if len(vectors) != len(batch):
            raise RuntimeError('Embedding 返回数量与请求不符')
        checked = []
        for vector in vectors:
            if len(vector) != dimensions:
                raise RuntimeError('Embedding 维度不正确')
            values = [float(v) for v in vector]
            if not all(math.isfinite(x) for x in values):
                raise RuntimeError('Embedding 存在 NaN/Inf')
            checked.append(values)
        collection.upsert(
            ids=[x['id'] for x in batch],
            documents=texts,
            embeddings=checked,
            metadatas=[_chunk_metadata(item, x, False) for x in batch],
        )
        embedded += len(batch)

    # 校验全部 Chunk 已存在，且原始文件/缓存未在本轮中途变更。
    verified = collection.get(where={'source': source}, include=['documents', 'metadatas'])
    by_id = {identifier: (content, metadata) for identifier, content, metadata in zip(
        verified['ids'], verified['documents'], verified['metadatas'])}
    for chunk in item['chunks']:
        if chunk['id'] not in by_id or by_id[chunk['id']][0] != chunk['content']:
            raise RuntimeError('Chroma 写入后验证失败，保留未激活的暂存向量')
    original = _checked_source(source_root, source)
    if (original is None or sha256_file(original) != row['sha256']
            or sha256_file(item['cache_path']) != item['cache_digest']):
        raise RuntimeError('索引期间原件或缓存发生变化，停止激活')

    # 激活当前版本；旧版本清理只在新版本完整写入后执行。
    for start in range(0, len(item['chunks']), batch_size):
        batch = item['chunks'][start:start + batch_size]
        collection.update(
            ids=[x['id'] for x in batch],
            metadatas=[_chunk_metadata(item, x, True) for x in batch],
        )
    obsolete = list(set(by_id) - expected)
    if obsolete:
        collection.delete(ids=obsolete)

    final = collection.get(where={'source': source}, include=['metadatas'])
    if set(final['ids']) != expected or not all(m.get('is_active') is True for m in final['metadatas']):
        raise RuntimeError('激活后 Chroma 数据不一致')

    vector_db.execute('''
        INSERT INTO vector_documents
        (source_path, source_sha256, parser_version, chunker_version,
         document_id, chunk_ids_json, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_path) DO UPDATE SET
          source_sha256=excluded.source_sha256,
          parser_version=excluded.parser_version,
          chunker_version=excluded.chunker_version,
          document_id=excluded.document_id,
          chunk_ids_json=excluded.chunk_ids_json,
          indexed_at=excluded.indexed_at
    ''', (source, row['sha256'], row['parser_version'], row['chunker_version'],
          row['document_id'], json.dumps(item['ids']),
          datetime.now(timezone.utc).isoformat()))
    vector_db.commit()
    return embedded


def sync_vectors(*, source_root: Path, chunk_dir: Path, chunk_db: Path,
                 vector_db: Path, source_filter: str | None = None,
                 write: bool = False, model: str = 'test-model',
                 dimensions: int = 4, collection=None, embed_texts=None,
                 batch_size: int = BATCH_SIZE) -> dict:
    """预览无需注入依赖；写入可注入 FakeCollection 和 Fake Embedding 测试。"""
    if not 1 <= batch_size <= 64:
        raise ValueError('batch_size 必须在 1～64')
    summary, plan = build_plan(source_root=source_root, chunk_dir=chunk_dir,
                               chunk_db=chunk_db, vector_db=vector_db,
                               source_filter=source_filter)
    summary['preview_only'] = not write
    summary['newly_embedded_chunks'] = 0
    if not write:
        return summary
    if summary['failed'] or summary['pending_parse'] or summary['chunks_requiring_review']:
        raise RuntimeError('入库前检查未通过：存在旧缓存、失败或需要人工复核的文档')
    if collection is None or embed_texts is None:
        raise ValueError('写入时必须提供 Chroma Collection 和 Embedding 函数')
    validate_collection(collection, model, dimensions)
    # Fail closed if an excluded file somehow already exists in this Collection.
    # Do not silently delete it or continue processing until manually reviewed.
    for excluded in EXCLUDED_SOURCES:
        prior = collection.get(where={'source': excluded}, include=['metadatas'])
        if prior.get('ids'):
            raise RuntimeError('发现已被明确排除的来源存在历史向量：停止写入，请人工检查并清理')
    vector_db = Path(vector_db)
    vector_db.parent.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    with sqlite3.connect(vector_db) as db:
        db.execute(CREATE_SQL)
        db.commit()
        # 即便 SQLite 状态为 unchanged，也校验 Chroma 实际记录是否完整。
        for item in plan:
            try:
                summary['newly_embedded_chunks'] += _index_one(
                    collection, item, embed_texts, dimensions, batch_size, db,
                    Path(source_root).resolve(strict=True))
            except Exception as exc:
                summary['failed'] += 1
                summary['errors'].append({'source': item['row']['source'],
                                          'reason': f'{type(exc).__name__}: {exc}'})
    return summary


def _real_clients():
    """仅在用户显式 --write 时创建云端和 Chroma 客户端。"""
    import chromadb
    from dotenv import load_dotenv
    from openai import OpenAI

    from app.rag.chunk_sync import STORAGE_ROOT as EXPECTED_STORAGE_ROOT
    if not os.path.ismount(EXPECTED_STORAGE_ROOT):
        raise RuntimeError('Storage 未挂载，拒绝入库')
    load_dotenv(Path(__file__).resolve().parents[2] / '.env')
    model = os.environ['EMBEDDING_MODEL']
    dimensions = int(os.environ['EMBEDDING_DIMENSIONS'])
    if dimensions != 1024:
        raise RuntimeError('此阶段预期使用 1024 维向量，请检查模型配置')

    db = chromadb.PersistentClient(path=str(CHROMA_PATH))
    col = db.get_or_create_collection(name=COLLECTION_NAME,
                                      metadata=_collection_metadata(model, dimensions),
                                      embedding_function=None)
    validate_collection(col, model, dimensions)
    client = OpenAI(api_key=os.environ['EMBEDDING_API_KEY'],
                    base_url=os.environ['EMBEDDING_BASE_URL'],
                    timeout=60, max_retries=1)

    def embed(texts: list[str]) -> list[list[float]]:
        response = client.embeddings.create(model=model, input=texts,
                                            dimensions=dimensions)
        ordered = sorted(response.data, key=lambda x: x.index)
        if [x.index for x in ordered] != list(range(len(texts))):
            raise RuntimeError('Embedding 返回顺序或数量异常')
        return [x.embedding for x in ordered]

    return model, dimensions, col, embed


def main():
    parser = argparse.ArgumentParser(description='Storage 增量向量索引；默认只读预览')
    parser.add_argument('--write', action='store_true', help='真正请求百炼并写入 ChromaDB')
    parser.add_argument('--source', default=None, help='仅处理该 Storage 相对路径')
    parser.add_argument('--batch-size', type=int, default=BATCH_SIZE)
    args = parser.parse_args()
    if not os.path.ismount(STORAGE_ROOT):
        raise SystemExit('Storage 未挂载，停止执行')
    params = dict(source_root=STORAGE_ROOT, chunk_dir=CHUNK_DIR,
                  chunk_db=CHUNK_DB, vector_db=VECTOR_DB,
                  source_filter=args.source, batch_size=args.batch_size)
    if args.write:
        from app.rag.sync_lock import cli_writer_lock
        with cli_writer_lock(STORAGE_ROOT, write=True):
            result = _write_cli(params)
    else:
        result = sync_vectors(**params)
    print('模式：', '百炼 Embedding + ChromaDB' if args.write else '只读预览')
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result['failed'] or result['pending_parse']:
        raise SystemExit(2)


def _write_cli(params):
    # 在创建任何外部客户端前先完成排除名单和缓存质量检查。
    preflight, _ = build_plan(source_root=STORAGE_ROOT, chunk_dir=CHUNK_DIR,
                              chunk_db=CHUNK_DB, vector_db=VECTOR_DB,
                              source_filter=params['source_filter'])
    if (preflight['failed'] or preflight['pending_parse']
            or preflight['chunks_requiring_review']):
        raise SystemExit('预检查有异常，未调用云端 API；请先运行不带 --write 的预览')
    model, dimensions, col, embed = _real_clients()
    return sync_vectors(**params, write=True, model=model,
                        dimensions=dimensions, collection=col,
                        embed_texts=embed)


if __name__ == '__main__':
    main()
