
"""
文件：app/rag/vector_ingestion.py

用途：
    将已有 PDF 解析结果向量化并写入 ChromaDB。

流程：
    PDF 归档 Markdown
        → build_chunks()
        → 百炼 Embedding
        → ChromaDB

主要特性：
    1. 复用现有 PDF 分块结构。
    2. 默认预览，不调用云端 API。
    3. 分批向量化。
    4. 已存在的 Chunk 不重复向量化。
    5. 支持部分写入后的安全续跑。
    6. 严格检查 Embedding 模型、维度与分块配置。
    7. 不自动删除现有 ChromaDB 数据。

说明：
    本模块仅处理已归档的 PDF Markdown。
    暂不处理 Word、TXT 或普通 Markdown 笔记。
"""

import argparse
import json
import math
import os

from pathlib import Path

from app.rag.chunking import build_chunks


# =====================================
# 1. 基础配置
# =====================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

STORAGE_ROOT = Path("/home/sweet/Storage")
KB_ROOT = STORAGE_ROOT / "KnowledgeBase"
PAPER_ROOT = KB_ROOT / "documents" / "papers"
CHROMA_PATH = KB_ROOT / "chroma"

COLLECTION_NAME = "sweet_kb_papers_v1"

CHUNK_SIZE = 1000
EMBEDDING_BATCH_SIZE = 8


# =====================================
# 2. 检查已有论文归档
# =====================================

def prepare_paper(markdown_path: Path) -> dict:
    """
    读取已经归档的 PDF Markdown 和元数据。

    本函数不会修改任何文件或数据库，
    也不会调用 Embedding API。
    """

    paper_root = PAPER_ROOT.resolve(strict=True)

    markdown_path = Path(markdown_path).resolve(
        strict=True
    )

    # 当前只处理 papers 目录下的直接 Markdown 文件。
    if (
        markdown_path.parent != paper_root
        or markdown_path.suffix.lower() != ".md"
    ):
        raise ValueError(
            "只能索引知识库 papers 目录中的归档 Markdown"
        )

    metadata_path = (
        paper_root
        / "_metadata"
        / f"{markdown_path.stem}.json"
    )

    metadata = json.loads(
        metadata_path.read_text(encoding="utf-8")
    )

    document_id = metadata["document_id"]

    if document_id != markdown_path.stem:
        raise ValueError("文档 ID 与文件名称不一致")

    expected_markdown = (
        KB_ROOT / metadata["markdown_path"]
    ).resolve(strict=True)

    if expected_markdown != markdown_path:
        raise ValueError("Markdown 来源路径不一致")

    # 确保原始 PDF 归档仍然存在。
    archived_pdf = (
        KB_ROOT / metadata["archived_pdf"]
    ).resolve(strict=True)

    if (
        not archived_pdf.is_file()
        or archived_pdf.suffix.lower() != ".pdf"
        or not archived_pdf.is_relative_to(
            (PAPER_ROOT / "originals").resolve(
                strict=True
            )
        )
    ):
        raise ValueError("原始 PDF 归档不合法")

    file_sha256 = metadata["sha256"]

    if (
        not isinstance(file_sha256, str)
        or len(file_sha256) != 64
        or any(c not in "0123456789abcdef"
               for c in file_sha256.lower())
    ):
        raise ValueError("PDF SHA256 元数据无效")

    chunks, pages = build_chunks(
        markdown_path,
        max_chars=CHUNK_SIZE,
    )

    if not chunks:
        raise RuntimeError("没有有效文本块")

    chunk_ids = [item["id"] for item in chunks]

    if len(chunk_ids) != len(set(chunk_ids)):
        raise RuntimeError("存在重复 Chunk ID")

    for chunk in chunks:
        if (
            chunk["document_id"] != document_id
            or not chunk["content"].strip()
        ):
            raise RuntimeError("Chunk 内容或来源不一致")

    return {
        "document_id": document_id,
        "file_sha256": file_sha256,
        "metadata": metadata,
        "chunks": chunks,
        "page_count": len(pages),
    }


# =====================================
# 3. 检查 Collection 的配置
# =====================================

def validate_collection(
    collection,
    model: str,
    dimensions: int,
):
    """防止不同向量维度或分块策略混写。"""

    config = collection.metadata or {}

    expected = {
        "embedding_model": model,
        "embedding_dimensions": dimensions,
        "chunk_size": CHUNK_SIZE,
    }

    for key, value in expected.items():

        if config.get(key) != value:
            raise RuntimeError(
                f"Collection 配置不匹配：{key}"
            )


# =====================================
# 4. 分批向量化与写入
# =====================================

def index_chunks(
    collection,
    prepared: dict,
    embed_texts,
    *,
    model: str,
    dimensions: int,
    batch_size: int = EMBEDDING_BATCH_SIZE,
) -> dict:
    """
    将一篇论文的 Chunks 写入 ChromaDB。

    embed_texts：
        输入 list[str]。
        返回对应顺序的 list[list[float]]。

    可以注入模拟函数进行离线测试。
    """

    validate_collection(
        collection,
        model,
        dimensions,
    )

    if not 1 <= batch_size <= 64:
        raise ValueError("batch_size 必须在 1～64 之间")

    chunks = prepared["chunks"]
    document_id = prepared["document_id"]
    file_sha256 = prepared["file_sha256"]

    # =====================================
    # 4.1 查询这篇论文已有的 Chunk
    # =====================================

    existing = collection.get(
        where={"document_id": document_id},
        include=["metadatas"],
    )

    existing_ids = set(existing["ids"])

    expected_ids = {
        chunk["id"] for chunk in chunks
    }

    # 不能因为分块策略变化就悄悄删旧数据。
    unexpected_ids = existing_ids - expected_ids

    if unexpected_ids:
        raise RuntimeError(
            "检测到同一文档的旧版 Chunk，"
            "请先检查索引策略，不自动覆盖"
        )

    for record in existing.get("metadatas", []):

        if (
            not isinstance(record, dict)
            or record.get("file_sha256")
            != file_sha256
        ):
            raise RuntimeError(
                "已有向量来源元数据不一致"
            )

    missing = [
        chunk
        for chunk in chunks
        if chunk["id"] not in existing_ids
    ]

    # =====================================
    # 4.2 分批 Embedding
    # =====================================

    inserted = 0

    for start in range(0, len(missing), batch_size):

        batch = missing[
            start:start + batch_size
        ]

        texts = [
            chunk["content"]
            for chunk in batch
        ]

        vectors = embed_texts(texts)

        if len(vectors) != len(batch):
            raise RuntimeError(
                "Embedding 返回数量与请求不一致"
            )

        verified_vectors = []

        for vector in vectors:

            if len(vector) != dimensions:
                raise RuntimeError(
                    "Embedding 向量维度不正确"
                )

            values = [float(v) for v in vector]

            if not all(math.isfinite(v) for v in values):
                raise RuntimeError(
                    "Embedding 包含非有限数值"
                )

            verified_vectors.append(values)

        records = []

        for chunk in batch:

            records.append({
                "document_id": chunk["document_id"],
                "source": chunk["source"],
                "source_pdf": chunk["source_pdf"],
                "page_number": int(chunk["page_number"]),
                "chunk_index": int(chunk["chunk_index"]),
                "file_sha256": file_sha256,
            })

        # 成功生成、验证向量后才写入。
        collection.upsert(
            ids=[chunk["id"] for chunk in batch],
            documents=texts,
            embeddings=verified_vectors,
            metadatas=records,
        )

        inserted += len(batch)

    # =====================================
    # 4.3 验证最终入库情况
    # =====================================

    final = collection.get(
        where={"document_id": document_id},
        include=["metadatas"],
    )

    if set(final["ids"]) != expected_ids:
        raise RuntimeError(
            "ChromaDB 写入后校验失败"
        )

    return {
        "status": "completed",
        "collection": collection.name,
        "document_id": document_id,
        "chunk_total": len(chunks),
        "already_indexed": len(existing_ids),
        "newly_indexed": inserted,
    }


# =====================================
# 5. 调用真实百炼 Embedding
# =====================================

def index_paper(prepared: dict) -> dict:
    """连接真实 ChromaDB 和百炼 Embedding 服务。"""

    import chromadb

    from dotenv import load_dotenv
    from openai import OpenAI

    if not os.path.ismount(STORAGE_ROOT):
        raise RuntimeError("Storage 未挂载，停止入库")

    load_dotenv(PROJECT_ROOT / ".env")

    model = os.environ["EMBEDDING_MODEL"]
    dimensions = int(
        os.environ["EMBEDDING_DIMENSIONS"]
    )

    client = OpenAI(
        api_key=os.environ["EMBEDDING_API_KEY"],
        base_url=os.environ["EMBEDDING_BASE_URL"],
        timeout=60,
        max_retries=1,
    )

    database = chromadb.PersistentClient(
        path=str(CHROMA_PATH)
    )

    collection = database.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={
            "embedding_model": model,
            "embedding_dimensions": dimensions,
            "chunk_size": CHUNK_SIZE,
        },
        embedding_function=None,
    )

    validate_collection(
        collection,
        model,
        dimensions,
    )

    def embed_texts(texts: list[str]) -> list[list[float]]:
        """按输入顺序获取 Embedding 结果。"""

        response = client.embeddings.create(
            model=model,
            input=texts,
            dimensions=dimensions,
        )

        ordered = sorted(
            response.data,
            key=lambda item: item.index,
        )

        if [
            item.index for item in ordered
        ] != list(range(len(texts))):
            raise RuntimeError(
                "Embedding 结果索引异常"
            )

        return [
            item.embedding
            for item in ordered
        ]

    return index_chunks(
        collection,
        prepared,
        embed_texts,
        model=model,
        dimensions=dimensions,
    )


# =====================================
# 6. 命令行入口
# =====================================

def main():

    parser = argparse.ArgumentParser(
        description="Sweet Research Agent PDF 向量入库"
    )

    parser.add_argument(
        "--markdown",
        required=True,
        type=Path,
        help="已归档的 PDF Markdown 文件路径",
    )

    parser.add_argument(
        "--write",
        action="store_true",
        help="真正调用 Embedding 并写入 ChromaDB",
    )

    args = parser.parse_args()

    prepared = prepare_paper(args.markdown)

    print("========== 文档入库检查 ==========")
    print("文档 ID：", prepared["document_id"])
    print("PDF SHA256：", prepared["file_sha256"])
    print("解析页数：", prepared["page_count"])
    print("Chunk 数量：", len(prepared["chunks"]))
    print("Chunk 大小配置：", CHUNK_SIZE)
    print(
        "空文本页：",
        prepared["metadata"].get("empty_pages", []),
    )

    if not args.write:
        print("\n预览完成：未调用 Embedding，未写入数据库")
        return

    result = index_paper(prepared)

    print("\n========== 向量入库结果 ==========")
    print(json.dumps(
        result,
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
