"""12.7 检索层离线回归测试。不访问百炼、ChromaDB 或真实 Storage。"""
import json

from app.rag.retrieval import search_knowledge
from app.rag.vector_sync import _collection_metadata


MODEL = 'qwen3.7-text-embedding'
DIM = 1024
EXCLUDED = 'KnowledgeBase/documents/Diary/24.7.16.md'


class FakeCollection:
    def __init__(self, rows):
        self.metadata = _collection_metadata(MODEL, DIM)
        self.rows = rows
        self.queries = []

    def count(self):
        return len(self.rows)

    def query(self, *, query_embeddings, n_results, where, include):
        assert len(query_embeddings) == 1
        assert len(query_embeddings[0]) == DIM
        assert where == {'is_active': True}
        assert include == ['documents', 'metadatas', 'distances']
        self.queries.append((n_results, where))
        active = [r for r in self.rows if r['meta']['is_active']]
        active.sort(key=lambda r: r['distance'])
        active = active[:n_results]
        return {
            'ids': [[r['id'] for r in active]],
            'documents': [[r['text'] for r in active]],
            'metadatas': [[r['meta'] for r in active]],
            'distances': [[r['distance'] for r in active]],
        }


def make_row(id, source, distance, active=True, text='测试文本'):
    return dict(id=id, text=text, distance=distance,
                meta={'source': source, 'chunk_index': 1, 'is_active': active,
                      'document_id': 'doc-1', 'file_type': 'md',
                      'heading_path': '章节'})


def main():
    print('========== 1. 已激活与排除 ==========', flush=True)
    col = FakeCollection([
        make_row('inactive', 'note/inactive.md', 0.05, active=False),
        make_row('private', EXCLUDED, 0.1),
        make_row('note1', 'PC_Files/Files/笔记.txt', 1.042394),
        make_row('note2', 'KnowledgeBase/documents/notes/rag_intro.md', 1.107026),
    ])
    calls = []

    def fake_embed(question):
        calls.append(question)
        return [0.1] * DIM

    matches = search_knowledge('  RAG 相关笔记  ', top_k=2,
                               collection=col, embed_fn=fake_embed)
    assert len(matches) == 2 and calls == ['RAG 相关笔记']
    assert [m['chunk_id'] for m in matches] == ['note1', 'note2']
    assert all(m['source'] != EXCLUDED for m in matches)
    assert all('similarity' not in m for m in matches)
    assert matches[0]['distance'] == 1.042394
    assert matches[0]['heading_path'] == '章节'
    assert col.queries[0][1] == {'is_active': True}
    print('PASS：只检索激活向量、排除文件不可返回、来源可追踪', flush=True)

    print('========== 2. Top-K 和参数 ==========', flush=True)
    assert len(search_knowledge('问题', top_k=1, collection=col, embed_fn=fake_embed)) == 1
    for invalid in (0, 4, True, '2'):
        try:
            search_knowledge('问题', top_k=invalid, collection=col, embed_fn=fake_embed)
            raise AssertionError('无效 top_k 未被拒绝')
        except ValueError:
            pass
    try:
        search_knowledge('  ', collection=col, embed_fn=fake_embed)
        raise AssertionError('空问题未被拒绝')
    except ValueError:
        pass
    print('PASS：Top-K 和空问题检查', flush=True)

    print('========== 3. 向量与集合配置 ==========', flush=True)
    try:
        search_knowledge('问题', collection=col, embed_fn=lambda _: [0.1, 0.2])
        raise AssertionError('维度错误未被拒绝')
    except RuntimeError:
        pass
    bad = FakeCollection(col.rows)
    bad.metadata['embedding_dimensions'] = 768
    try:
        search_knowledge('问题', collection=bad, embed_fn=fake_embed)
        raise AssertionError('集合维度错误未被拒绝')
    except RuntimeError:
        pass
    print('PASS：向量维度与 Collection Schema 检查', flush=True)

    print('========== 4. 空集合与只读行为 ==========', flush=True)
    empty = FakeCollection([])
    never_embed = lambda _: (_ for _ in ()).throw(AssertionError('空集合不应调用 API'))
    assert search_knowledge('问题', collection=empty, embed_fn=never_embed) == []
    print('PASS：空集合不执行 Embedding', flush=True)

    print('========== 5. LangGraph 工具输出 ==========', flush=True)
    try:
        from app.tools import knowledge as mod
    except ModuleNotFoundError as exc:
        if exc.name == 'langchain_core':
            print('SKIP：本机未安装 langchain_core，服务器中应执行这项检查', flush=True)
        else:
            raise
    else:
        original = mod.search_knowledge
        try:
            mod.search_knowledge = lambda **kwargs: matches
            result = json.loads(mod.search_knowledge_tool.invoke(
                {'question': 'RAG 相关笔记', 'top_k': 2}))
            assert result['count'] == 2
            assert result['collection'] == 'sweet_kb_storage_v1'
            assert result['matches'][0]['chunk_id'] == 'note1'
            assert 'similarity' not in result['matches'][0]
            assert 'content_note' in result
            print('PASS：Agent 工具协议兼容、距离未伪装成相似度', flush=True)
        finally:
            mod.search_knowledge = original

    print('\nStorage 检索层测试完成', flush=True)


if __name__ == '__main__':
    main()
