
from langchain_core.messages import HumanMessage

from langgraph.checkpoint.memory import InMemorySaver

# 复用第10.4课定义好的 Graph 构建器
from learn_langgraph_04 import builder


# =====================================
# 1. 创建内存 Checkpointer
# =====================================

checkpointer = InMemorySaver()

# 重新编译，加入状态保存功能
graph = builder.compile(
    checkpointer=checkpointer
)


# =====================================
# 2. 配置两个独立对话
# =====================================

config_research = {
    "configurable": {
        "thread_id": "research-001"
    },
    "recursion_limit": 10
}

config_general = {
    "configurable": {
        "thread_id": "general-001"
    },
    "recursion_limit": 10
}


# =====================================
# 3. 封装多轮问答函数
# =====================================

def ask(question, config):

    thread_id = config["configurable"]["thread_id"]

    print("\n" + "=" * 60)
    print("Thread ID：", thread_id)
    print("用户：", question)

    # 每次只提交本轮新的用户消息
    result = graph.invoke(
        {
            "messages": [
                HumanMessage(content=question)
            ]
        },
        config=config
    )

    print("\n========== Agent 回答 ==========")
    print(result["messages"][-1].content) #取最后一个消息，也就是最新的：AIMessage("AI最新回答")

    print(
        "\n当前 State 消息总数：",
        len(result["messages"])
    )


# =====================================
# 4. 第一轮：私人知识库查询
# =====================================

ask(
    "根据我的知识库，RAG 与 Agent "
    "有什么关系？请注明来源。",
    config_research
)


# =====================================
# 5. 第二轮：继续刚才的对话
# =====================================

ask(
    "那它们分别是什么？"
    "请继续依据之前的知识库资料回答。",
    config_research
)


# =====================================
# 6. 第三个问题：新对话线程
# =====================================

ask(
    "Python 中的列表和元组有什么区别？",
    config_general
)


# =====================================
# 7. 检查两个线程各自的状态
# =====================================

print("\n" + "=" * 60)
print("========== Checkpointer 状态检查 ==========")

for config in [config_research, config_general]:

    thread_id = config["configurable"]["thread_id"]

    # 读取该对话线程的最新 State
    snapshot = graph.get_state(config)

    messages = snapshot.values.get("messages", [])

    user_messages = [
        m for m in messages
        if isinstance(m, HumanMessage)
    ]

    print(f"\nThread ID：{thread_id}")
    print("State 消息数量：", len(messages))
    print("用户提问次数：", len(user_messages))

    for i, message in enumerate(
        user_messages, start=1
    ):
        print(f"  第{i}轮：{message.content}")


print("\n========== 测试完成 ==========")
