"""12.6 增量向量索引离线测试；使用临时目录和 FakeCollection，不联网。"""
import hashlib
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from app.rag.document_chunking import build_document_chunks
from app.rag.vector_sync import (
    EXCLUDED_SOURCES, _collection_metadata, build_plan, sync_vectors,
)

MODEL, DIM = 'test-embedding', 4
PRIVATE = 'KnowledgeBase/documents/Diary/24.7.16.md'


class FakeCollection:
    name = 'test-storage'

    def __init__(self):
        self.metadata = _collection_metadata(MODEL, DIM)
        self.items = {}

    def get(self, *, where, include):
        rows = [(key, item) for key, item in self.items.items()
                if item['metadata']['source'] == where['source']]
        return {'ids': [key for key, _ in rows],
                'documents': [value['content'] for _, value in rows],
                'metadatas': [value['metadata'].copy() for _, value in rows]}

    def upsert(self, *, ids, documents, embeddings, metadatas):
        for key, text, vector, metadata in zip(ids, documents, embeddings, metadatas):
            self.items[key] = {'content': text, 'vector': vector,
                               'metadata': metadata.copy()}

    def update(self, *, ids, metadatas):
        for key, metadata in zip(ids, metadatas):
            if key not in self.items:
                raise RuntimeError('unknown id')
            self.items[key]['metadata'] = metadata.copy()

    def delete(self, *, ids):
        for key in ids:
            self.items.pop(key, None)


class FakeEmbedder:
    def __init__(self):
        self.calls = []
        self.fail_after = None

    def __call__(self, texts):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise RuntimeError('模拟网络中断')
        self.calls.append(len(texts))
        return [[float(len(s)), 1., 2., 3.] for s in texts]


def add_doc(root: Path, source: str, content: str, *, review=False):
    src = root / 'Storage' / source
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(content, encoding='utf-8')
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    record = {'document_id': hashlib.sha256((source + '\0' + sha).encode()).hexdigest()[:32],
              'source': source, 'sha256': sha, 'file_type': 'md',
              'content': content, 'warnings': ['REVIEW'] if review else []}
    chunks = build_document_chunks(record, max_chars=1000, overlap_chars=120)
    name = hashlib.sha256((source + sha).encode()).hexdigest() + '.json'
    cache = root / 'chunk_cache' / name
    cache.parent.mkdir(exist_ok=True)
    payload = {'source': source, 'sha256': sha,
               'parser_version': 'document_loader_v1',
               'chunker_version': 'document_chunking_v1:max=1000:overlap=120',
               'document_id': record['document_id'], 'chunk_count': len(chunks),
               'review_count': sum(x['review_required'] for x in chunks),
               'chunks': chunks}
    cache.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    with sqlite3.connect(root / 'chunk_sync.sqlite3') as db:
        db.execute('''INSERT INTO chunk_cache
          (source_path, source_sha256, parser_version, chunker_version, document_id,
           chunk_count, review_count, cache_path)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(source_path) DO UPDATE SET
           source_sha256=excluded.source_sha256,
           parser_version=excluded.parser_version,
           chunker_version=excluded.chunker_version,
           document_id=excluded.document_id,
           chunk_count=excluded.chunk_count,
           review_count=excluded.review_count,
           cache_path=excluded.cache_path''',
           (source, sha, payload['parser_version'], payload['chunker_version'],
            payload['document_id'], len(chunks), payload['review_count'], str(cache)))
    return chunks


def main():
    assert PRIVATE in EXCLUDED_SOURCES
    with TemporaryDirectory() as temp:
        root = Path(temp)
        (root / 'Storage').mkdir()
        with sqlite3.connect(root / 'chunk_sync.sqlite3') as db:
            db.execute('''CREATE TABLE chunk_cache (
                source_path TEXT PRIMARY KEY, source_sha256 TEXT, parser_version TEXT,
                chunker_version TEXT, document_id TEXT, chunk_count INTEGER,
                review_count INTEGER, cache_path TEXT)''')
        a = 'PC_Files/Uploads/test.md'
        other_diary = 'KnowledgeBase/documents/Diary/24.7.17.md'
        add_doc(root, a, '# 测试\n\n多头注意力适合特征融合。')
        add_doc(root, PRIVATE, '# Private\n\n只用于本地缓存。')
        add_doc(root, other_diary, '# 可索引日记\n\n这是一份测试文字。')
        params = dict(source_root=root/'Storage', chunk_dir=root/'chunk_cache',
                      chunk_db=root/'chunk_sync.sqlite3', vector_db=root/'vector.sqlite3')
        collection = FakeCollection()
        embed = FakeEmbedder()
        def write(**opts):
            return sync_vectors(**params, write=True, model=MODEL,
                                dimensions=DIM, collection=collection,
                                embed_texts=embed, **opts)
        print('========== 1. 排除与预览 ==========', flush=True)
        s = sync_vectors(**params)
        assert s['total'] == 2 and s['excluded'] == 1 and s['new'] == 2
        assert not (root/'vector.sqlite3').exists()
        assert not collection.items and not embed.calls
        try:
            sync_vectors(**params, source_filter=PRIVATE)
            raise AssertionError('被排除的文档未被阻止')
        except ValueError as exc:
            assert '排除' in str(exc)
        print('PASS：仅排除指定文件，默认预览不联网不写入', flush=True)

        print('========== 2. 首次写入 ==========', flush=True)
        s = write()
        assert s['failed'] == 0 and s['newly_embedded_chunks'] == 2
        assert len(collection.items) == 2
        assert all(v['metadata']['source'] != PRIVATE for v in collection.items.values())
        assert all(v['metadata']['is_active'] is True for v in collection.items.values())
        with sqlite3.connect(root/'vector.sqlite3') as db:
            sources = {r[0] for r in db.execute('SELECT source_path FROM vector_documents')}
        assert sources == {a, other_diary}
        print('PASS：两个授权文档入库，指定私密文件未上传', flush=True)

        print('========== 3. 重复同步 ==========', flush=True)
        embed.calls.clear()
        s = write()
        assert s['failed'] == 0 and s['unchanged'] == 2
        assert s['newly_embedded_chunks'] == 0 and not embed.calls
        print('PASS：未变化不重复调用 Embedding', flush=True)

        print('========== 4. 修改后更新 ==========', flush=True)
        old_a = {k for k, v in collection.items.items() if v['metadata']['source'] == a}
        old_diary = {k for k, v in collection.items.items() if v['metadata']['source'] == other_diary}
        add_doc(root, a, '# 测试\n\n这是更新后的注意力研究。')
        s = write()
        assert s['modified'] == 1 and s['failed'] == 0
        assert s['newly_embedded_chunks'] == 1
        assert not old_a.intersection(collection.items)
        assert old_diary.issubset(collection.items)
        assert all(v['metadata']['is_active'] for v in collection.items.values())
        print('PASS：修改后建立新向量、替换旧块', flush=True)

        print('========== 5. 宕机续跑 ==========', flush=True)
        newtext = '# 长段落\n\n' + ('可检索段落 ' * 250)
        latest = add_doc(root, a, newtext)
        assert len(latest) > 1
        prev_active = {k for k, v in collection.items.items()
                       if v['metadata']['source'] == a and v['metadata']['is_active']}
        embed.calls.clear()
        embed.fail_after = 1
        failed = write(source_filter=a, batch_size=1)
        assert failed['failed'] == 1
        assert prev_active.issubset(set(collection.items))
        assert any(not v['metadata']['is_active'] for v in collection.items.values())
        embed.fail_after = None
        embed.calls.clear()
        recovered = write(source_filter=a, batch_size=1)
        assert recovered['failed'] == 0
        assert recovered['newly_embedded_chunks'] == len(latest) - 1
        assert {v['metadata']['source'] for v in collection.items.values()} == {a, other_diary}
        assert all(v['metadata']['is_active'] for v in collection.items.values())
        print('PASS：中断后仅补齐缺失向量，再完成切换', flush=True)

        print('========== 6. 过期源文件 ==========', flush=True)
        (root/'Storage'/a).write_text('不在缓存中的修改', encoding='utf-8')
        result = sync_vectors(**params, source_filter=a)
        assert result['pending_parse'] == 1
        assert result['errors'] and not any(PRIVATE in str(e) for e in result['errors'])
        print('PASS：源文件变更未重新分块时停止', flush=True)

        print('========== 7. 质量警告 ==========', flush=True)
        c = 'PC_Files/Uploads/review.md'
        add_doc(root, c, '# 公式待复核\n\n内容。', review=True)
        try:
            write(source_filter=c)
            raise AssertionError('质量警告未拦截')
        except RuntimeError as exc:
            assert '检查未通过' in str(exc)
        print('PASS：质量警告拦截真实写入', flush=True)

        print('========== 8. 配置不一致 ==========', flush=True)
        collection.metadata['embedding_dimensions'] = 5
        try:
            write(source_filter=other_diary)
            raise AssertionError('维度不一致未拦截')
        except RuntimeError as exc:
            assert '维度' in str(exc)
        print('PASS：不同模型或维度拒绝混写', flush=True)

        print('========== 9. 旧索引中出现已排除文件 ==========', flush=True)
        collection.metadata = _collection_metadata(MODEL, DIM)
        collection.items['private-old'] = {
            'content': '模拟旧数据', 'vector': [1., 2., 3., 4.],
            'metadata': {'source': PRIVATE, 'is_active': True},
        }
        before = len(embed.calls)
        try:
            write(source_filter=other_diary)
            raise AssertionError('已排除文件的旧索引未被识别')
        except RuntimeError as exc:
            assert '历史向量' in str(exc)
        assert len(embed.calls) == before
        print('PASS：发现已排除文件的历史向量时停止写入', flush=True)
    print('\n增量向量索引离线测试全部通过', flush=True)

if __name__ == '__main__':
    main()
