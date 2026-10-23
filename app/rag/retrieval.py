"""文件：app/rag/retrieval.py

12.7 Storage RAG 检索层。

只查询 12.6 已建立的 sweet_kb_storage_v1：
- 只返回 is_active=True 的 Chunk；
- 二次排除明确不允许入库的指定来源；
- 使用真实 Chroma 距离，不把 1-distance 当成相似度；
- 返回来源路径、Chunk ID、编号、标题与文件版本，便于溯源；
- 不创建/修改 Chroma Collection，不向量化文档；问题本身需要云端 Embedding。

测试可通过 collection 和 embed_fn 注入模拟依赖，避免联网。
"""

import math
import os
from pathlib import Path
from typing import Callable

from app.rag.vector_sync import (
    CHROMA_PATH,
    COLLECTION_NAME,
    EXCLUDED_SOURCES,
    SCHEMA,
    STORAGE_ROOT,
    validate_collection,
)

EXPECTED_DIMENSIONS = 1024
MAX_TOP_K = 3
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _open_real_collection():
    """仅在真实检索时打开已有集合；缺失时直接报错，绝不创建空库。"""
    if not os.path.ismount(STORAGE_ROOT):
        raise RuntimeError('Storage 未挂载，拒绝知识库检索')

    import chromadb
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / '.env')
    model = os.environ['EMBEDDING_MODEL']
    dimensions = int(os.environ['EMBEDDING_DIMENSIONS'])
    if dimensions != EXPECTED_DIMENSIONS:
        raise RuntimeError('检索 Embedding 维度与当前知识库约定不符')

    collection = chromadb.PersistentClient(path=str(CHROMA_PATH)).get_collection(
        name=COLLECTION_NAME,
        embedding_function=None,
    )
    validate_collection(collection, model, dimensions)
    return collection


def _embed_question(question: str):
    """复用已通过真实测试的 query Embedding；不发送任何文档正文。"""
    from scripts.search_knowledge import embed_query
    return embed_query(question)


def search_knowledge(
    question: str,
    top_k: int = 2,
    *,
    collection=None,
    embed_fn: Callable[[str], list[float]] | None = None,
) -> list[dict]:
    """从正式 Storage 集合检索并返回可溯源的片段。

    source 是相对于 /home/sweet/Storage 的路径。
    distance 是 Chroma 距离（越小越接近），不是相似度百分比。
    可选参数仅为离线测试依赖注入；Agent 仍只传 question/top_k。
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError('检索问题不能为空')
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_TOP_K:
        raise ValueError('top_k 必须在 1 到 3 之间')

    question = question.strip()
    col = collection if collection is not None else _open_real_collection()

    # 离线测试及真实环境都进行基础结构校验；真实环境额外核对模型名。
    metadata = getattr(col, 'metadata', None)
    if (not isinstance(metadata, dict)
            or metadata.get('index_schema') != SCHEMA
            or metadata.get('embedding_dimensions') != EXPECTED_DIMENSIONS):
        raise RuntimeError('Storage 向量集合的 Schema 或维度不符合要求')

    count = col.count()
    if count == 0:
        return []

    vector = (embed_fn or _embed_question)(question)
    if (len(vector) != EXPECTED_DIMENSIONS
            or not all(math.isfinite(float(value)) for value in vector)):
        raise RuntimeError('查询向量维度错误或包含非有限数值')

    # 适度超取：让二次排除/格式校验后仍有足够候选。
    result = col.query(
        query_embeddings=[[float(value) for value in vector]],
        n_results=min(count, max(top_k * 5, top_k)),
        where={'is_active': True},
        include=['documents', 'metadatas', 'distances'],
    )

    ids = (result.get('ids') or [[]])[0]
    documents = (result.get('documents') or [[]])[0]
    metadatas = (result.get('metadatas') or [[]])[0]
    distances = (result.get('distances') or [[]])[0]

    matches = []
    for chunk_id, content, meta, distance in zip(ids, documents, metadatas, distances):
        if not isinstance(meta, dict) or meta.get('is_active') is not True:
            continue
        source = meta.get('source')
        if not isinstance(source, str) or not source or source in EXCLUDED_SOURCES:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        if not isinstance(distance, (int, float)) or not math.isfinite(distance):
            continue

        match = {
            'source': source,
            'chunk_id': str(chunk_id),
            'chunk_index': meta.get('chunk_index'),
            'distance': round(float(distance), 6),
            'content': content,
        }
        # 这些字段均由 12.6 的 _chunk_metadata() 实际写入。
        for field in ('document_id', 'file_sha256', 'file_type', 'heading_path'):
            value = meta.get(field)
            if value is not None and value != '':
                match[field] = value
        matches.append(match)
        if len(matches) >= top_k:
            break

    return matches
