"""文件：tests/test_document_chunking.py；不访问真实知识库或网络。"""
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from app.rag.document_chunking import build_document_chunks, preview_cached_documents


def make_record(content: str, *, docid: str = 'doc-001', warnings=None):
    return {'document_id': docid, 'source': 'PC_Files/Uploads/research.md',
            'sha256': 'a' * 64, 'file_type': 'md', 'content': content,
            'warnings': warnings or []}


def main():
    print('========== 1. 标题、段落 =========')
    document = make_record('# SD-LoRA\n\n第一段说明。\n\n第二段讨论。\n\n## 消融实验\n\n实验结果正常。')
    chunks = build_document_chunks(document, max_chars=110, overlap_chars=12)
    assert chunks and all(len(c['content']) <= 110 for c in chunks)
    assert any('SD-LoRA / 消融实验' == c['heading_path'] for c in chunks)
    assert chunks[0]['content'].startswith('# SD-LoRA')
    assert chunks[0]['source'] == document['source']
    print('PASS：标题层级、段落与来源保留')

    print('\n========== 2. 稳定 ID =========')
    again = build_document_chunks(document, max_chars=110, overlap_chars=12)
    assert [c['id'] for c in chunks] == [c['id'] for c in again]
    changed = make_record(document['content'].replace('实验结果正常', '实验结果已更新'))
    changed_ids = [c['id'] for c in build_document_chunks(changed, max_chars=110, overlap_chars=12)]
    assert changed_ids != [c['id'] for c in chunks]
    print('PASS：相同输入稳定，内容变化影响分块 ID')

    print('\n========== 3. 长段落和重叠 =========')
    long = make_record('A' * 450)
    long_chunks = build_document_chunks(long, max_chars=120, overlap_chars=20)
    assert len(long_chunks) >= 4
    assert all(len(c['content']) <= 120 for c in long_chunks)
    for i in range(1, len(long_chunks)):
        overlap = long_chunks[i]['overlap_chars']
        if overlap:
            assert long_chunks[i]['content'][:overlap] == long_chunks[i-1]['content'][-overlap:]
    # 逆重叠拼接，保证不会丢失原始段落非空白字符。
    reconstructed = long_chunks[0]['content']
    for c in long_chunks[1:]:
        reconstructed += c['content'][c['overlap_chars'] + (1 if c['overlap_chars'] else 0):]
    assert reconstructed == long['content']
    print('PASS：限制长度、重叠正确且不会丢失长段落')

    print('\n========== 4. Word 表格与质量标志 =========')
    table = make_record('# 实验\n\n| 方法 | mIoU |\n|---|---|\n| SD-LoRA | 50.37 |')
    tchunks = build_document_chunks(table, max_chars=180, overlap_chars=10)
    assert '| SD-LoRA | 50.37 |' in '\n'.join(c['content'] for c in tchunks)
    flagged = make_record('正文内容', warnings=['DOCX_DRAWINGS_NOT_EXTRACTED:2'])
    fchunks = build_document_chunks(flagged)
    assert fchunks[0]['review_required'] and fchunks[0]['warnings']
    print('PASS：表格保留，解析警告阻止静默视为完整')

    print('\n========== 5. SQLite 缓存预览只读 =========')
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        cache = root / 'cache.json'
        cache.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
        database = root / 'sync.sqlite3'
        db = sqlite3.connect(database)
        db.execute('CREATE TABLE parsed_documents (source_path TEXT, parsed_path TEXT)')
        db.execute('INSERT INTO parsed_documents VALUES (?, ?)', (document['source'], str(cache)))
        db.commit()
        db.close()
        before = database.read_bytes()
        summary = preview_cached_documents(database)
        after = database.read_bytes()
        assert summary['documents'] == 1 and summary['chunk_total'] >= 1
        assert summary['preview_only'] is True and not summary['failed_documents']
        assert before == after
    print('PASS：只读预览已有解析缓存，不写入数据库')
    print('\n文档分块测试全部通过')


if __name__ == '__main__':
    main()
