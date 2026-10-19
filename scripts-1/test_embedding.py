
import os
import math
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


# =====================================
# 1. 读取环境配置
# =====================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

API_KEY = os.getenv("EMBEDDING_API_KEY")
BASE_URL = os.getenv("EMBEDDING_BASE_URL")
MODEL = os.getenv(
    "EMBEDDING_MODEL",
    "qwen3.7-text-embedding"
)
DIMENSIONS = int(
    os.getenv("EMBEDDING_DIMENSIONS", "1024")
)

if not API_KEY or not BASE_URL:
    raise RuntimeError("请检查 .env 中的 Embedding API 配置")

if "这里填写" in API_KEY or "这里填写" in BASE_URL:
    raise RuntimeError("请先替换 .env 中的占位内容")


# =====================================
# 2. 创建 Embedding API 客户端
# =====================================

client = OpenAI(
    api_key=API_KEY,
    base_url=BASE_URL,
    timeout=60.0,
    max_retries=1
)


# =====================================
# 3. 准备三段测试文本
# =====================================

texts = [
    "SD-LoRA在ADE20K数据集上进行语义分割实验。",
    "使用ADE20K数据集评估SD-LoRA的分割性能。",
    "今天的天气很好，适合到公园散步。"
]


# =====================================
# 4. 调用云端 Embedding API
# =====================================

print("========== Embedding API 测试 ==========")
print("模型：", MODEL)
print("设置维度：", DIMENSIONS)

response = client.embeddings.create(
    model=MODEL,
    input=texts,
    dimensions=DIMENSIONS
)


# =====================================
# 5. 提取返回的向量
# =====================================

vectors = [
    item.embedding
    for item in sorted(
        response.data,
        key=lambda item: item.index
    )
]

print("\n========== 返回结果 ==========")

for i, vector in enumerate(vectors):
    print(f"\n文本 {i + 1}：{texts[i]}")
    print(f"向量维度：{len(vector)}")
    print(f"前五个数值：{vector[:5]}")

    assert len(vector) == DIMENSIONS, "向量维度不匹配"


# =====================================
# 6. 计算余弦相似度
# =====================================

def cosine_similarity(a, b):

    dot = sum(x * y for x, y in zip(a, b))

    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))

    if norm_a == 0 or norm_b == 0:
        raise ValueError("向量范数不能为0")

    return dot / (norm_a * norm_b)


similar_12 = cosine_similarity(vectors[0], vectors[1])
similar_13 = cosine_similarity(vectors[0], vectors[2])

print("\n========== 语义相似度 ==========")
print(f"文本1 vs 文本2：{similar_12:.4f}")
print(f"文本1 vs 文本3：{similar_13:.4f}")


# =====================================
# 7. 输出 Token 使用量
# =====================================

if response.usage:
    print("\n========== Token 用量 ==========")
    print("总 Token：", response.usage.total_tokens)

print("\nEmbedding API 测试成功！")
