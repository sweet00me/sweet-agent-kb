from uuid import uuid4

from langchain_core.messages import HumanMessage
from app.agents.graph import build_graph
from app.memory.checkpoint import open_checkpointer

questions = [
    "根据我的知识库，RAG 与 Agent 有什么关系？请引用来源。",
    "Python 列表和元组有什么区别？简短回答。",
    "我希望 GPU 留给语义分割训练，PDF 解析以后支持 CPU/GPU 切换。",
    "回忆第一轮讨论的两个概念，并重新检索知识库核实它们的关系。"
]

thread_id = "summary-test-" + uuid4().hex[:8]

config = {
    "configurable": {"thread_id": thread_id},
    "recursion_limit": 12
}

print("测试线程：", thread_id)

with open_checkpointer() as saver:

    graph = build_graph(
        checkpointer=saver,
        enable_summarization=True
    )

    for i, question in enumerate(questions, 1):

        print("\n" + "=" * 50)
        print("第", i, "轮：", question)

        result = graph.invoke(
            {"messages": [HumanMessage(content=question)]},
            config=config
        )

        print("Agent：", result["messages"][-1].content)
        print("活动消息数量：", len(result["messages"]))
        print("已有摘要：", bool(result.get("summary")))

    snapshot = graph.get_state(config)

    print("\n========== 最终摘要 ==========")
    print(snapshot.values.get("summary", "无"))

    print(
        "\n最终活动消息数：",
        len(snapshot.values["messages"])
    )