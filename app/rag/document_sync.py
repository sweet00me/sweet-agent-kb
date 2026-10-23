"""Storage 文档增量解析：只缓存解析结果，不进行 Embedding。"""
import argparse
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.rag.document_loader import load_document

STORAGE_ROOT = Path('/home/sweet/Storage')
KB_ROOT = STORAGE_ROOT / 'KnowledgeBase'
PARSED_DIR = KB_ROOT / 'parsed_documents'
STATE_DB = KB_ROOT / 'sqlite' / 'document_sync.sqlite3'
PARSER_VERSION = 'document_loader_v1'
MAX_BYTES = 20 * 1024 * 1024
MAX_SCANNED_FILES = 100_000
EXTENSIONS = {'.md', '.txt', '.docx'}
EXCLUDE_PREFIXES = (
    'KnowledgeBase/chroma',
    'KnowledgeBase/sqlite',
    'KnowledgeBase/backups',
    'KnowledgeBase/reports',
    'KnowledgeBase/documents/papers',
    'KnowledgeBase/parsed_documents',
    'Datasets',
    'Checkpoints',
)
EXCLUDE_NAMES = {'lost+found', '__pycache__', 'node_modules', '.git'}
# =====================================
# 不参与知识库解析的指定文件
# =====================================

EXCLUDE_FILES = {
    "PC_Files/shared/other/稳重单页25.docx",
    "PC_Files/shared/other/简历_1.docx",
    "PC_Files/shared/other/简约单页18.docx",
}


def is_excluded(relative: str) -> bool:
    return any(relative == p or relative.startswith(p + '/') for p in EXCLUDE_PREFIXES)



def scan_documents(root: Path) -> list[str]:
    """
    扫描 Storage 中符合条件的文档。

    功能：
        1. 排除指定目录和文件。
        2. 跳过隐藏文件、符号链接和其他磁盘。
        3. 限制扫描数量和文件大小。
        4. 返回相对于 Storage 的文件路径。

    安全：
        只要发生目录访问异常，就拒绝将扫描结果
        视为完整结果。
    """

    root = Path(root).resolve(strict=True)

    found = []
    errors = []
    scanned = 0

    root_device = root.stat().st_dev

    def onerror(error):
        errors.append(str(error))

    for current, dirs, files in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=onerror,
    ):

        parent = Path(current)

        # =============================
        # 1. 筛选允许进入的目录
        # =============================

        allowed_dirs = []

        for name in dirs:

            path = parent / name
            relative = path.relative_to(root).as_posix()

            if (
                name.startswith(".")
                or name in EXCLUDE_NAMES
                or is_excluded(relative)
            ):
                continue

            try:
                info = path.stat(
                    follow_symlinks=False
                )

                if (
                    stat.S_ISDIR(info.st_mode)
                    and info.st_dev == root_device
                ):
                    allowed_dirs.append(name)

            except OSError as exc:
                errors.append(str(exc))

        dirs[:] = sorted(allowed_dirs)

        # =============================
        # 2. 筛选文件
        # =============================

        for name in files:

            path = parent / name
            relative = path.relative_to(root).as_posix()

            # 只排除指定文件
            if relative in EXCLUDE_FILES:
                continue

            scanned += 1

            if scanned > MAX_SCANNED_FILES:
                raise RuntimeError(
                    "扫描文件数超出安全上限"
                )

            if (
                name.startswith(".")
                or path.suffix.lower() not in EXTENSIONS
            ):
                continue

            try:
                info = path.stat(
                    follow_symlinks=False
                )

                if (
                    stat.S_ISREG(info.st_mode)
                    and info.st_dev == root_device
                    and 0 < info.st_size <= MAX_BYTES
                ):
                    found.append(relative)

            except OSError as exc:
                errors.append(str(exc))

    # 注意：这两条语句必须位于 os.walk 循环外面。
    if errors:
        raise RuntimeError(
            f"扫描不完整，出现 {len(errors)} 个访问异常"
        )

    return sorted(found)



def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def open_state(db_path: Path, write: bool):
    if not write and not db_path.exists():
        return None
    if write:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.execute('''
            CREATE TABLE IF NOT EXISTS parsed_documents (
                source_path TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                parsed_path TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        ''')
        connection.commit()
        return connection
    return sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)


def save_json_atomic(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=path.parent, prefix='.tmp-',
                                         suffix='.json', delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sync_storage(root: Path, output_dir: Path, db_path: Path, *, commit=False):
    """返回同步统计。默认只读；commit=True 才保存解析缓存和状态。"""
    root = root.resolve(strict=True)
    paths = scan_documents(root)
    db = open_state(db_path, write=commit)
    try:
        old = {}
        if db is not None:
            for rel, sha, version, parsed in db.execute(
                'SELECT source_path, sha256, parser_version, parsed_path FROM parsed_documents'
            ):
                old[rel] = (sha, version, parsed)
        summary = {'new': 0, 'modified': 0, 'unchanged': 0,
                   'failed': 0, 'missing': len(set(old) - set(paths)),
                   'total': len(paths)}

        for rel in paths:
            source = root / rel
            try:
                digest = sha256_file(source)
                stable_name = hashlib.sha256(rel.encode('utf-8')).hexdigest() + '.json'
                destination = output_dir / stable_name
                previous = old.get(rel)
                if (previous is not None and previous[0] == digest
                        and previous[1] == PARSER_VERSION
                        and destination.is_file()):
                    summary['unchanged'] += 1
                    continue

                status = 'new' if previous is None else 'modified'
                if not commit:
                    summary[status] += 1
                    continue

                result = load_document(rel, documents_root=root)
                if result['sha256'] != digest or sha256_file(source) != digest:
                    raise RuntimeError('解析期间源文件发生变化')
                result['source'] = rel  # 以整个 Storage 为根，纠正旧 loader 的默认前缀
                result['parser_version'] = PARSER_VERSION
                save_json_atomic(destination, result)
                db.execute('''
                    INSERT INTO parsed_documents
                    (source_path, sha256, parser_version, parsed_path, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(source_path) DO UPDATE SET
                      sha256=excluded.sha256,
                      parser_version=excluded.parser_version,
                      parsed_path=excluded.parsed_path,
                      updated_at=excluded.updated_at
                ''', (rel, digest, PARSER_VERSION, str(destination),
                      datetime.now(timezone.utc).isoformat()))
                db.commit()
                summary[status] += 1
            except Exception as exc:
                summary['failed'] += 1
                print(f'[FAILED] {rel}: {type(exc).__name__}: {exc}')

        return summary
    finally:
        if db is not None:
            db.close()


def main():
    parser = argparse.ArgumentParser(description='Storage 文档增量解析（默认预览）')
    parser.add_argument('--parse', action='store_true', help='实际解析并保存到本地')
    args = parser.parse_args()
    if not os.path.ismount(STORAGE_ROOT):
        raise SystemExit('Storage 未挂载，停止处理')
    if args.parse:
        os.umask(0o077)
    from app.rag.sync_lock import cli_writer_lock
    with cli_writer_lock(STORAGE_ROOT, write=args.parse):
        summary = sync_storage(STORAGE_ROOT, PARSED_DIR, STATE_DB, commit=args.parse)
    print('模式：', '本地解析（不向量化）' if args.parse else '只读预览')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
