
import os
import argparse
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from openai import OpenAI


# =====================================
# 1. 项目路径和配置
# =====================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORAGE_ROOT = Path("/home/sweet/Storage")
KB_ROOT = STORAGE_ROOT / "KnowledgeBase"
DB_PATH = KB_ROOT / "chroma"

COLLECTION_NAME = "sweet_kb_documents_v1"

# 防止 Storage 未挂载时读错数据库
if not os.path.ismount(STORAGE_ROOT):
    raise RuntimeError("Storage 未挂载，停止运行")

load_dotenv(PROJECT_ROOT / ".env")

MODEL = os.environ["EMBEDDING_MODEL"]
DIMENSIONS = int(os.environ["EMBEDDING_DIMENSIONS"])


# =====================================
# 2. 创建云端 Embedding 客户端
# =====================================

embedding_client = OpenAI(
    api_key=os.environ["EMBEDDING_API_KEY"],
    base_url=os.environ["EMBEDDING_BASE_URL"],
    timeout=60.0,
    max_retries=1
)


# =====================================
# 3. 连接已经存在的 ChromaDB
# =====================================

db = chromadb.PersistentClient(
    path=str(DB_PATH)
)

collection = db.get_collection(
    name=COLLECTION_NAME,
    embedding_function=None
)

# 确认向量模型和维度一致
config = collection.metadata or {}

if (
    config.get("embedding_model") != MODEL
    or config.get("embedding_dimensions") != DIMENSIONS
):
    raise RuntimeError(
        "Embedding 配置与已有知识库不一致"
    )


# =====================================
# 4. 将问题转换成向量
# =====================================

def embed_query(question):
    """使用云端 Embedding API 向量化问题。"""

    response = embedding_client.embeddings.create(
        model=MODEL,
        input=[question],
        dimensions=DIMENSIONS
    )

    if len(response.data) != 1:
        raise RuntimeError("查询向量数量不正确")

    vector = response.data[0].embedding

    if len(vector) != DIMENSIONS:
        raise RuntimeError("查询向量维度不正确")

    return vector


# =====================================
# 5. 封装知识库检索函数
# =====================================

def search_knowledge(question, top_k=3):
    """
    输入自然语言问题，返回最相关的文档片段。

    参数：
        question: 用户问题
        top_k: 希望返回的最大结果数

    返回：
        包含内容、来源、相似度的字典列表
    """

    if not question.strip():
        raise ValueError("问题不能为空")

    if top_k < 1:
        raise ValueError("top_k 必须大于等于 1")

    # 检查知识库是否为空
    total = collection.count()

    if total == 0:
        return []

    # 第一步：将用户问题向量化
    query_vector = embed_query(question)

    # 第二步：使用 ChromaDB 搜索
    results = collection.query(
        query_embeddings=[query_vector],
        n_results=min(top_k, total),
        include=[
            "documents",
            "metadatas",
            "distances"
        ]
    )

    # 第三步：整理检索结果
    records = []

    for doc_id, document, metadata, distance in zip(
        results["ids"][0],
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0]
    ):
        records.append({
            "id": doc_id,
            "content": document,
            "source": metadata.get(
                "source", "unknown"
            ),
            "chunk_index": metadata.get(
                "chunk_index", -1
            ),
            "similarity": 1.0 - distance
        })

    return records


# =====================================
# 6. 命令行测试入口
# =====================================

def main():

    parser = argparse.ArgumentParser(
        description="搜索 sweetserver 私人知识库"
    )

    parser.add_argument(
        "question",
        type=str,
        help="需要检索的问题"
    )

    parser.add_argument(
        "--top-k",
        type=int,
        default=3,
        help="返回的最大结果数，默认3"
    )

    args = parser.parse_args()

    print("========== 知识库检索 ==========")
    print("用户问题：", args.question)
    print("Collection：", COLLECTION_NAME)
    print("知识库总 Chunk：", collection.count())

    records = search_knowledge(
        question=args.question,
        top_k=args.top_k
    )

    if not records:
        print("\n知识库为空，没有可检索的内容。")
        return

    print("\n========== 检索结果 ==========")

    for i, record in enumerate(records, start=1):

        print(f"\n---------- 排名 {i} ----------")

        print("来源：", record["source"])
        print("Chunk 编号：", record["chunk_index"])
        print(
            "相似度：",
            f'{record["similarity"]:.4f}'
        )

        print("\n文档内容：")
        print(record["content"])

    print("\n检索完成。")


if __name__ == "__main__":
    main()
