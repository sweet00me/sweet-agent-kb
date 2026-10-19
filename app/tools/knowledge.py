
import json

from langchain_core.tools import tool

from app.rag.retrieval import search_knowledge


@tool
def search_knowledge_tool(
    question: str,
    top_k: int = 2
) -> str:
    """
    检索用户私人科研知识库中的文档内容。

    当需要查询用户的论文笔记、知识库资料、
    已入库的科研记录时使用。

    Args:
        question: 需要检索的具体问题。
        top_k: 返回的片段数量，范围为1到3。
    """

    question = question.strip()

    if not question:
        raise ValueError("检索问题不能为空")

    if not 1 <= top_k <= 3:
        raise ValueError("top_k 必须在1到3之间")

    records = search_knowledge(
        question=question,
        top_k=top_k
    )

    matches = []

    for record in records:

        matches.append({
            "source": record["source"],
            "chunk_index": record["chunk_index"],
            "similarity": round(
                float(record["similarity"]), 4
            ),
            "content": record["content"]
        })

    result = {
        "count": len(matches),
        "matches": matches
    }

    return json.dumps(
        result,
        ensure_ascii=False
    )
