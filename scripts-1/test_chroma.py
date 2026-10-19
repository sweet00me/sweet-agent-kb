
import os
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from openai import OpenAI


# =====================================
# 1. 基础配置
# =====================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

STORAGE_ROOT = Path("/home/sweet/Storage")
DB_PATH = STORAGE_ROOT / "KnowledgeBase/chroma"

# 防止 Storage 未挂载时误写入系统分区
if not os.path.ismount(STORAGE_ROOT):
    raise RuntimeError("Storage 未正确挂载，停止运行")

load_dotenv(PROJECT_ROOT / ".env")

MODEL = os.environ["EMBEDDING_MODEL"]
DIMENSIONS = int(os.environ["EMBEDDING_DIMENSIONS"])

embedding_client = OpenAI(
    api_key=os.environ["EMBEDDING_API_KEY"],
    base_url=os.environ["EMBEDDING_BASE_URL"],
    timeout=60.0,
    max_retries=1
)


# =====================================
# 2. 定义云端向量化函数
# =====================================

def get_embeddings(texts):
    """调用百炼 API 获取文本向量。"""

    response = embedding_client.embeddings.create(
        model=MODEL,
        input=texts,
        dimensions=DIMENSIONS
    )

    items = sorted(
        response.data,
        key=lambda x: x.index
    )

    vectors = [item.embedding for item in items]

    if len(vectors) != len(texts):
        raise RuntimeError("返回向量数量不正确")

    if any(len(v) != DIMENSIONS for v in vectors):
        raise RuntimeError("返回向量维度不正确")

    return vectors


# =====================================
# 3. 连接本地 ChromaDB
# =====================================

DB_PATH.mkdir(parents=True, exist_ok=True)

db = chromadb.PersistentClient(
    path=str(DB_PATH)
)

collection = db.get_or_create_collection(
    name="sweet_kb_demo_v1",
    embedding_function=None,
    configuration={
        "hnsw": {
            "space": "cosine"
        }
    },
    metadata={
        "embedding_model": MODEL,
        "embedding_dimensions": DIMENSIONS
    }
)

# 防止意外混用其他模型生成的向量
metadata = collection.metadata or {}

if (
    metadata.get("embedding_model") != MODEL
    or metadata.get("embedding_dimensions") != DIMENSIONS
):
    raise RuntimeError("现有集合的 Embedding 配置不匹配")


# =====================================
# 4. 准备测试文档
# =====================================

texts = [
    "SD-LoRA在ADE20K数据集上进行语义分割实验。",
    "使用ADE20K数据集评估SD-LoRA的分割性能。",
    "今天的天气很好，适合到公园散步。"
]

ids = [
    "demo-001",
    "demo-002",
    "demo-003"
]

metadatas = [
    {"source": "embedding_demo", "category": "research"},
    {"source": "embedding_demo", "category": "research"},
    {"source": "embedding_demo", "category": "daily"}
]


# =====================================
# 5. 向量化并写入数据库
# =====================================

print("正在调用云端 Embedding API...")

vectors = get_embeddings(texts)

collection.upsert(
    ids=ids,
    documents=texts,
    embeddings=vectors,
    metadatas=metadatas
)

print("\n========== 数据写入成功 ==========")
print("集合名称：", collection.name)
print("记录数量：", collection.count())
print("数据库目录：", DB_PATH)


# =====================================
# 6. 测试语义检索
# =====================================

question = "有哪些关于SD-LoRA分割性能的实验？"

print("\n========== 用户问题 ==========")
print(question)

# 注意：查询问题也需要使用相同模型向量化
query_vector = get_embeddings([question])[0]

results = collection.query(
    query_embeddings=[query_vector],
    n_results=3,
    include=["documents", "metadatas", "distances"]
)


# =====================================
# 7. 输出检索结果
# =====================================

print("\n========== ChromaDB 检索结果 ==========")

for i, (doc, distance, meta) in enumerate(
    zip(
        results["documents"][0],
        results["distances"][0],
        results["metadatas"][0]
    ),
    start=1
):
    similarity = 1.0 - distance

    print(f"\n排名：{i}")
    print(f"内容：{doc}")
    print(f"余弦相似度：{similarity:.4f}")
    print(f"来源：{meta['source']}")

print("\nChromaDB 测试完成！")
