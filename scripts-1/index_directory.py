"""
有两种运行方式：
python index_markdown.py --dry-run # 仅检查变化，不调用 Embedding API 程序只打印需要新增或更新的文件列表
python index_markdown.py             # 实际索引 Markdown 文件 如果文件新增或修改，程序会继续调用 Embedding API 并更新 ChromaDB。
"""


import os
import hashlib
import argparse
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from openai import OpenAI


# =====================================
# 1. 基础配置
# =====================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE_ROOT = Path("/home/sweet/Storage")
KB_ROOT = STORAGE_ROOT / "KnowledgeBase"

DOC_ROOT = (KB_ROOT / "documents").resolve()
DB_PATH = KB_ROOT / "chroma"

COLLECTION_NAME = "sweet_kb_documents_v1"

CHUNK_SIZE = 240
CHUNK_OVERLAP = 40

# 学习阶段限制单个文件的大小和片段数
MAX_FILE_BYTES = 256 * 1024
MAX_CHUNKS_PER_FILE = 200

if not os.path.ismount(STORAGE_ROOT):
    raise RuntimeError("Storage 未挂载，停止运行")  

load_dotenv(PROJECT_ROOT / ".env")

MODEL = os.environ["EMBEDDING_MODEL"]
DIMENSIONS = int(os.environ["EMBEDDING_DIMENSIONS"])

api_client = OpenAI(
    api_key=os.environ["EMBEDDING_API_KEY"],
    base_url=os.environ["EMBEDDING_BASE_URL"],
    timeout=60.0,
    max_retries=1
)


# =====================================
# 2. 文本切分
# =====================================

def split_text(text):
    text = text.strip()

    chunks = []
    start = 0

    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))

        if end < len(text):
            cut = text.rfind(
                "\n",
                start + CHUNK_SIZE // 2,
                end
            )

            if cut != -1:
                end = cut + 1

        chunk = text[start:end].strip()

        if chunk:
            chunks.append(chunk)

        if end >= len(text):
            break

        start = end - CHUNK_OVERLAP

    return chunks


# =====================================
# 3. 调用云端 Embedding API
# =====================================

def get_embeddings(texts):
    vectors = []

    # 每批最多处理16个片段
    for start in range(0, len(texts), 16):
        batch = texts[start:start + 16]

        response = api_client.embeddings.create(
            model=MODEL,
            input=batch,
            dimensions=DIMENSIONS
        )

        items = sorted(
            response.data,
            key=lambda item: item.index
        )

        batch_vectors = [
            item.embedding for item in items
        ]

        if len(batch_vectors) != len(batch):
            raise RuntimeError("Embedding 数量不匹配")

        if any(len(v) != DIMENSIONS for v in batch_vectors):
            raise RuntimeError("Embedding 维度不匹配")

        vectors.extend(batch_vectors)

    return vectors


# =====================================
# 4. 连接已有 ChromaDB
# =====================================

db = chromadb.PersistentClient(
    path=str(DB_PATH)
)

collection = db.get_collection(
    name=COLLECTION_NAME,
    embedding_function=None
)

config = collection.metadata or {}

if (
    config.get("embedding_model") != MODEL
    or config.get("embedding_dimensions") != DIMENSIONS
    or config.get("chunk_size") != CHUNK_SIZE
    or config.get("chunk_overlap") != CHUNK_OVERLAP
):
    raise RuntimeError("Collection 配置不匹配")


# =====================================
# 5. 处理单个 Markdown 文件
# =====================================

def index_file(path, dry_run=False):

    source = path.relative_to(DOC_ROOT).as_posix()

    if path.stat().st_size > MAX_FILE_BYTES:
        print(f"[跳过] {source}：文件过大")
        return "skipped"

    file_bytes = path.read_bytes()

    try:
        content = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        print(f"[跳过] {source}：不是 UTF-8 文本")
        return "skipped"

    if not content.strip():
        print(f"[跳过] {source}：空文件")
        return "skipped"

    file_hash = hashlib.sha256(
        file_bytes
    ).hexdigest()

    chunks = split_text(content)

    if len(chunks) > MAX_CHUNKS_PER_FILE:
        print(f"[跳过] {source}：Chunk 数量过多")
        return "skipped"

    source_hash = hashlib.sha256(
        source.encode("utf-8")
    ).hexdigest()[:16]

    ids = [
        f"{source_hash}-{file_hash[:16]}-{i:04d}"
        for i in range(len(chunks))
    ]

    # 查询当前文件已有的索引
    existing = collection.get(
        where={"source": source},
        include=["metadatas"]
    )

    #检查数据库中这个文件的所有 Chunk，其记录的文件哈希是否都等于当前文件的哈希。
    old_ids = set(existing["ids"])
    new_ids = set(ids)
    old_metadata = existing["metadatas"] or []

    unchanged = (
        old_ids == new_ids
        and all(
            meta.get("file_sha256") == file_hash
            for meta in old_metadata
        )
    )

    if unchanged:
        print(f"[未变化] {source}")
        return "unchanged" #直接出去函数

    # action = "更新" if old_ids else "新增"
    if old_ids:
        action = "更新"
    else:
        action = "新增"

    if dry_run:  #只检查程序准备进行哪些操作，但不真正修改数据库
        print(
            f"[待{action}] {source} "
            f"({len(chunks)} chunks)"
        )
        return "pending"

    # 先完成向量化，再写入
    vectors = get_embeddings(chunks)

    metadatas = [
        {
            "source": source,
            "file_sha256": file_hash,
            "chunk_index": i,
            "chunk_total": len(chunks)
        }
        for i in range(len(chunks))
    ]

    collection.upsert(
        ids=ids,
        documents=chunks,
        embeddings=vectors,
        metadatas=metadatas
    )

    # 删除该文件旧版本的片段
    obsolete_ids = list(old_ids - new_ids)

    if obsolete_ids:
        collection.delete(ids=obsolete_ids)

    print(
        f"[{action}成功] {source} "
        f"({len(chunks)} chunks)"
    )

    return "updated"


# =====================================
# 6. 扫描整个 documents 目录
# =====================================

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅检查变化，不调用 Embedding API"
    )

    args = parser.parse_args()

    files = sorted(
        path
        for path in DOC_ROOT.rglob("*.md")
        if path.is_file()
        and path.resolve().is_relative_to(DOC_ROOT)
    )

    print("========== Markdown 自动扫描 ==========")
    print("扫描目录：", DOC_ROOT)
    print("发现文件：", len(files))
    print("预览模式：", args.dry_run)

    stats = {
        "unchanged": 0,
        "updated": 0,
        "pending": 0,
        "skipped": 0,
        "failed": 0
    }

    for path in files:
        try:
            status = index_file(
                path,
                dry_run=args.dry_run
            )
            stats[status] += 1

        except Exception as exc:
            stats["failed"] += 1
            print(
                f"[失败] {path.name}: "
                f"{type(exc).__name__}: {exc}"
            )

    print("\n========== 扫描统计 ==========")

    for key, value in stats.items():
        print(f"{key}: {value}")

    print("当前 Collection 记录数：", collection.count())

    # 本版本不自动删除已经不在目录中的文件索引
    indexed = collection.get(
        include=["metadatas"]
    )

    indexed_sources = {
        meta["source"]
        for meta in indexed["metadatas"]
    }

    current_sources = {
        path.relative_to(DOC_ROOT).as_posix()
        for path in files
    }

    missing = indexed_sources - current_sources

    if missing:
        print("\n注意：发现源文件已不存在的索引：")
        for source in sorted(missing):
            print("  ", source)

        print("本次不自动删除这些历史索引。")


if __name__ == "__main__":
    main()
