
"""
文件：tests/test_vector_ingestion.py

用途：
    验证科研论文向量化入库的核心逻辑。

验证：
    1. 首次入库。
    2. 同一文档重复入库。
    3. 部分失败后续跑。
    4. 向量维度异常拦截。
    5. Collection 配置冲突拦截。

不访问真实知识库和云端 Embedding API。
"""

from app.rag.vector_ingestion import (
    CHUNK_SIZE,
    index_chunks,
)


MODEL = "test-embedding"
DIMENSIONS = 4


class FakeCollection:
    """模拟必要的 ChromaDB 操作。"""

    name = "test_papers"

    def __init__(self):

        self.metadata = {
            "embedding_model": MODEL,
            "embedding_dimensions": DIMENSIONS,
            "chunk_size": CHUNK_SIZE,
        }

        self.records = {}

    def get(self, *, where, include):

        document_id = where["document_id"]

        selected = [
            (key, value)
            for key, value in self.records.items()
            if value["metadata"]["document_id"]
            == document_id
        ]

        return {
            "ids": [key for key, _ in selected],
            "metadatas": [
                value["metadata"]
                for _, value in selected
            ],
        }

    def upsert(
        self,
        *,
        ids,
        documents,
        embeddings,
        metadatas,
    ):

        for key, document, vector, metadata in zip(
            ids,
            documents,
            embeddings,
            metadatas,
        ):
            self.records[key] = {
                "document": document,
                "vector": vector,
                "metadata": metadata,
            }


def make_prepared():

    chunks = []

    for index in range(3):

        chunks.append({
            "id": f"chunk-{index}",
            "document_id": "paper-test-001",
            "source": "documents/papers/test.md",
            "source_pdf": (
                "documents/papers/originals/test.pdf"
            ),
            "page_number": index + 1,
            "chunk_index": index,
            "content": f"第 {index+1} 页的实验内容",
        })

    return {
        "document_id": "paper-test-001",
        "file_sha256": "a" * 64,
        "chunks": chunks,
    }


def main():

    collection = FakeCollection()
    prepared = make_prepared()

    calls = []

    def fake_embed(texts):

        calls.append(len(texts))

        return [
            [float(len(text)), 1.0, 2.0, 3.0]
            for text in texts
        ]

    print("========== 1. 首次入库 ==========")

    result = index_chunks(
        collection,
        prepared,
        fake_embed,
        model=MODEL,
        dimensions=DIMENSIONS,
        batch_size=2,
    )

    assert result["newly_indexed"] == 3
    assert len(collection.records) == 3
    assert calls == [2, 1]

    print("PASS：三个 Chunk 正确入库")

    print("\n========== 2. 重复入库 ==========")

    calls.clear()

    result = index_chunks(
        collection,
        prepared,
        fake_embed,
        model=MODEL,
        dimensions=DIMENSIONS,
    )

    assert result["newly_indexed"] == 0
    assert result["already_indexed"] == 3
    assert calls == []

    print("PASS：重复入库没有调用 Embedding")

    print("\n========== 3. 部分失败后续跑 ==========")

    # 模拟已经保存第一个 Chunk 的情况。
    del collection.records["chunk-1"]
    del collection.records["chunk-2"]

    calls.clear()

    result = index_chunks(
        collection,
        prepared,
        fake_embed,
        model=MODEL,
        dimensions=DIMENSIONS,
        batch_size=2,
    )

    assert result["already_indexed"] == 1
    assert result["newly_indexed"] == 2
    assert len(collection.records) == 3
    assert calls == [2]

    print("PASS：仅补齐缺失的两个 Chunk")

    print("\n========== 4. 向量维度校验 ==========")

    collection.records.clear()

    def invalid_embed(texts):
        return [[1.0, 2.0] for _ in texts]

    try:

        index_chunks(
            collection,
            prepared,
            invalid_embed,
            model=MODEL,
            dimensions=DIMENSIONS,
        )

        raise AssertionError("维度异常没有被拦截")

    except RuntimeError as exc:
        assert "维度" in str(exc)

    assert len(collection.records) == 0

    print("PASS：错误维度不会写入数据库")

    print("\n========== 5. Collection 配置 ==========")

    collection.metadata["embedding_dimensions"] = 1024

    try:

        index_chunks(
            collection,
            prepared,
            fake_embed,
            model=MODEL,
            dimensions=DIMENSIONS,
        )

        raise AssertionError("配置冲突没有被拦截")

    except RuntimeError as exc:
        assert "配置不匹配" in str(exc)

    print("PASS：不同 Embedding 配置不能混写")

    print("\n向量入库逻辑测试全部通过")


if __name__ == "__main__":
    main()
