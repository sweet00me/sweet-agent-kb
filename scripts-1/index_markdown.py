import sys
import os
import hashlib
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from openai import OpenAI


# =====================================
# 1. 路径与环境配置
# =====================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE_ROOT = Path("/home/sweet/Storage")
KB_ROOT = STORAGE_ROOT / "KnowledgeBase"


DOC_ROOT = (KB_ROOT / "documents").resolve()
DB_PATH = KB_ROOT / "chroma"

# 允许通过命令行指定要索引的 Markdown 文件
if len(sys.argv) > 2:
    raise SystemExit(
        "用法：python scripts/index_markdown.py [相对文件路径]"
    )

relative_path = Path(
    sys.argv[1]
    if len(sys.argv) == 2
    else "notes/rag_intro.md"
)

# 将相对路径转换为绝对路径
DOC_PATH = (DOC_ROOT / relative_path).resolve()

# 安全检查：只允许读取 documents 目录内的 Markdown
if not DOC_PATH.is_relative_to(DOC_ROOT):
    raise ValueError("不允许索引 documents 目录以外的文件")

if DOC_PATH.suffix.lower() != ".md":
    raise ValueError("当前脚本只支持 Markdown 文件")



COLLECTION_NAME = "sweet_kb_documents_v1"

CHUNK_SIZE = 240
CHUNK_OVERLAP = 40

if not os.path.ismount(STORAGE_ROOT):
    raise RuntimeError("Storage 未挂载，停止运行")

if not DOC_PATH.is_file():
    raise FileNotFoundError(DOC_PATH)

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

def split_text(text, chunk_size=240, overlap=40):
    """将文本切分为带有少量重叠的片段。"""

    if not 0 <= overlap < chunk_size:
        raise ValueError("overlap 必须小于 chunk_size")

    text = text.strip()
    chunks = []
    start = 0

    while start < len(text):
        end = min(start + chunk_size, len(text))

        # 尽量在换行处切分
        if end < len(text):
            cut = text.rfind(
                "\n",
                start + chunk_size // 2,
                end
            )
            if cut != -1:
                end = cut + 1

        chunk = text[start:end].strip()

        if chunk:
            chunks.append(chunk)

        if end >= len(text):
            break

        start = end - overlap

    return chunks


# =====================================
# 3. 云端 Embedding
# =====================================

def get_embeddings(texts):
    """使用百炼 API 批量生成向量。"""

    response = api_client.embeddings.create(
        model=MODEL,
        input=texts,
        dimensions=DIMENSIONS
    )

    items = sorted(response.data, key=lambda x: x.index)
    vectors = [item.embedding for item in items]

    if len(vectors) != len(texts):
        raise RuntimeError("向量数量不匹配")

    if any(len(v) != DIMENSIONS for v in vectors):
        raise RuntimeError("向量维度不匹配")

    return vectors


# =====================================
# 4. 读取文档、生成哈希
# =====================================

file_bytes = DOC_PATH.read_bytes()
content = file_bytes.decode("utf-8")

file_hash = hashlib.sha256(file_bytes).hexdigest()

source = DOC_PATH.relative_to(DOC_ROOT).as_posix()
source_hash = hashlib.sha256(
    source.encode("utf-8")
).hexdigest()[:16]

chunks = split_text(
    content,
    chunk_size=CHUNK_SIZE,
    overlap=CHUNK_OVERLAP
)

if not chunks:
    raise RuntimeError("文档内容为空")

# ID 包含来源、内容哈希和片段编号
ids = [
    f"{source_hash}-{file_hash[:16]}-{i:04d}"
    for i in range(len(chunks))
]

print("========== 文档信息 ==========")
print("来源：", source)
print("字符数：", len(content))
print("切分数量：", len(chunks))
print("SHA-256：", file_hash[:16], "...")


# =====================================
# 5. 连接 ChromaDB
# =====================================

db = chromadb.PersistentClient(path=str(DB_PATH))

collection = db.get_or_create_collection(
    name=COLLECTION_NAME,
    embedding_function=None,
    configuration={
        "hnsw": {"space": "cosine"}
    },
    metadata={
        "embedding_model": MODEL,
        "embedding_dimensions": DIMENSIONS,
        "chunk_size": CHUNK_SIZE,
        "chunk_overlap": CHUNK_OVERLAP
    }
)

config = collection.metadata or {}

if (
    config.get("embedding_model") != MODEL
    or config.get("embedding_dimensions") != DIMENSIONS
    or config.get("chunk_size") != CHUNK_SIZE
    or config.get("chunk_overlap") != CHUNK_OVERLAP
):
    raise RuntimeError("集合配置不匹配，请检查索引版本")


# =====================================
# 6. 检查是否需要更新
# =====================================

existing = collection.get(
    where={"source": source},
    include=["metadatas"]
)

old_ids = set(existing["ids"])
new_ids = set(ids)

if old_ids == new_ids:
    print("\n文件未变化，跳过文档向量化。")

else:
    print("\n文件新增或发生变化，正在更新索引...")

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

    # 先写入新片段
    collection.upsert(
        ids=ids,
        documents=chunks,
        embeddings=vectors,
        metadatas=metadatas
    )

    # 再删除当前文件的旧版本片段
    obsolete_ids = list(old_ids - new_ids)

    if obsolete_ids:
        collection.delete(ids=obsolete_ids)

    print("索引更新成功。")


# =====================================
# 7. 测试语义检索
# =====================================

question = "RAG 与 Agent 有什么关系？"

query_vector = get_embeddings([question])[0]

results = collection.query(
    query_embeddings=[query_vector],
    n_results=min(3, collection.count()),
    include=["documents", "metadatas", "distances"]
)

print("\n========== 检索结果 ==========")
print("问题：", question)

for i, (doc, meta, distance) in enumerate(
    zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0]
    ),
    start=1
):
    print(f"\n排名：{i}")
    print("来源：", meta["source"])
    print("片段编号：", meta["chunk_index"])
    print("相似度：", round(1.0 - distance, 4))
    print("内容：", doc[:180])

print("\n集合总记录数：", collection.count())
print("测试完成。")
