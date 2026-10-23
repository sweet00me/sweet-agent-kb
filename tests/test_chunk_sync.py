"""文件：tests/test_chunk_sync.py；仅临时目录，不进行云端调用。"""
import hashlib
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from app.rag.chunk_sync import sync_chunks


def run(root: Path, *, write=False, max_chars=1000, overlap_chars=120):
    return sync_chunks(
        source_root=root / 'Storage', parsed_root=root / 'parsed',
        source_db=root / 'source.sqlite3', cache_dir=root / 'chunks',
        chunk_db=root / 'chunks.sqlite3', write=write,
        max_chars=max_chars, overlap_chars=overlap_chars,
    )


def add_doc(root: Path, source: str, text: str):
    source_path = root / 'Storage' / source
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(text, encoding='utf-8')
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    parsed_path = root / 'parsed' / (hashlib.sha256(source.encode()).hexdigest() + '.json')
    parsed_path.parent.mkdir(parents=True, exist_ok=True)
    record = {'document_id': f'doc-{digest[:16]}', 'source': source, 'sha256': digest,
              'file_type': 'md', 'content': text, 'warnings': []}
    parsed_path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
    with sqlite3.connect(root / 'source.sqlite3') as db:
        db.execute('''INSERT INTO parsed_documents
            (source_path, sha256, parser_version, parsed_path)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_path) DO UPDATE SET sha256=excluded.sha256,
              parser_version=excluded.parser_version, parsed_path=excluded.parsed_path''',
                   (source, digest, 'loader_v1', str(parsed_path)))
    return source_path


def main():
    with TemporaryDirectory() as temp:
        root = Path(temp)
        (root / 'Storage').mkdir()
        (root / 'parsed').mkdir()
        with sqlite3.connect(root / 'source.sqlite3') as db:
            db.execute('''CREATE TABLE parsed_documents (
                source_path TEXT PRIMARY KEY, sha256 TEXT, parser_version TEXT,
                parsed_path TEXT)''')
        path = add_doc(root, 'notes/test.md', '# 研究\n\n第一段。\n\n第二段。')

        print('========== 1. 预览只读 ==========')
        preview = run(root)
        assert preview['new'] == 1 and preview['chunk_total'] >= 1
        assert not (root / 'chunks').exists()
        assert not (root / 'chunks.sqlite3').exists()
        print('PASS：未创建 Chunk 缓存或状态数据库')

        print('\n========== 2. 首次同步及重复跳过 ==========')
        first = run(root, write=True)
        assert first['new'] == 1 and first['chunk_total'] >= 1
        count = len(list((root / 'chunks').glob('*.json')))
        assert count == 1
        twice = run(root, write=True)
        assert twice['unchanged'] == 1 and twice['new'] == 0
        assert len(list((root / 'chunks').glob('*.json'))) == 1
        print('PASS：首次保存、重复运行不重新写入')

        print('\n========== 3. 修改后必须先重新解析 ==========')
        path.write_text('# 研究\n\n内容发生变化。', encoding='utf-8')
        pending = run(root, write=True)
        assert pending['pending_parse'] == 1 and pending['modified'] == 0
        add_doc(root, 'notes/test.md', '# 研究\n\n内容发生变化。')
        updated = run(root, write=True)
        assert updated['modified'] == 1
        assert len(list((root / 'chunks').glob('*.json'))) == 2
        print('PASS：拒绝旧解析，更新后生成新版本，不覆盖旧缓存')

        print('\n========== 4. 分块策略变化重新缓存 ==========')
        config_changed = run(root, write=True, max_chars=400, overlap_chars=40)
        assert config_changed['modified'] == 1
        print('PASS：分块策略变化触发缓存更新')

        print('\n========== 5. 文件删除不自动清空历史缓存 ==========')
        path.unlink()
        missing = run(root, write=True)
        assert missing['missing'] == 1 and missing['unchanged'] == 0
        assert len(list((root / 'chunks').glob('*.json'))) == 3
        print('PASS：删除仅识别为缺失，不执行破坏性清理')

    print('\n增量 Chunk 缓存测试全部通过')


if __name__ == '__main__':
    main()
