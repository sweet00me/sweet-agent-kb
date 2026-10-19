
from langchain_core.messages import (
    HumanMessage,
    AIMessage,
    ToolMessage,
)

# 直接复用上一课已经创建的 Graph
from learn_langgraph_04 import graph


# =====================================
# 1. 准备用户问题
# =====================================

question = (
    "我的 SD-LoRA 在 ADE20K 上的最终 "
    "mIoU 是多少？只根据知识库回答。"
)

print("========== 用户问题 ==========")
print(question)


# =====================================
# 2. 流式执行 LangGraph
# =====================================

events = graph.stream(
    {
        "messages": [
            HumanMessage(content=question)
        ]
    },
    config={
        "recursion_limit": 10
    },
    stream_mode="updates"
)


# =====================================
# 3. 观察每个 Node 的输出
# =====================================

for step, event in enumerate(events, start=1):

    print("\n" + "=" * 55)
    print(f"第 {step} 个执行事件")

    for node_name, node_output in event.items():

        print("执行节点：", node_name)

        messages = node_output.get("messages", [])

        for message in messages:

            # -------------------------
            # AIMessage
            # -------------------------

            if isinstance(message, AIMessage):

                if message.tool_calls:

                    print("[AIMessage] 请求工具")

                    for call in message.tool_calls:
                        print(
                            "  工具名称：",
                            call["name"]
                        )
                        print(
                            "  工具参数：",
                            call["args"]
                        )

                else:

                    print("[AIMessage] 最终回答：")
                    print(message.content)

                if message.usage_metadata:
                    print(
                        "[Token]",
                        message.usage_metadata.get(
                            "total_tokens", 0
                        )
                    )

            # -------------------------
            # ToolMessage
            # -------------------------

            elif isinstance(message, ToolMessage):

                print("[ToolMessage] 工具执行完成")
                print("工具名称：", message.name)
                print("Tool Call ID：", message.tool_call_id)

                # 仅预览工具结果，避免日志过长
                print(
                    "结果预览：",
                    str(message.content)[:180]
                )


print("\n========== Graph 执行结束 ==========")
