
from typing import TypedDict, NotRequired, Literal

from langgraph.graph import (
    StateGraph,
    START,
    END
)


# =====================================
# 1. 定义 State
# =====================================

class AgentState(TypedDict):
    question: str
    prepared_question: NotRequired[str]
    route: NotRequired[str]
    result: NotRequired[str]


# =====================================
# 2. Node 1：预处理问题
# =====================================

def prepare_question(state: AgentState):

    print("\n[Node] prepare_question")

    question = state["question"].strip()

    if not question:
        raise ValueError("问题不能为空")

    return {
        "prepared_question": question
    }


# =====================================
# 3. 条件路由函数
# =====================================

def choose_route(
    state: AgentState
) -> Literal["knowledge", "general"]:

    question = state["prepared_question"].lower()

    # 演示用关键词规则
    keywords = [
        "lrformer",
        "sd-lora",
        "ade20k",
        "我的知识库",
        "我的论文",
        "我的实验"
    ]

    if any(word in question for word in keywords):
        print("[Router] 选择知识库路径")
        return "knowledge"

    print("[Router] 选择普通问答路径")
    return "general"


# =====================================
# 4. Node 2A：知识库处理
# =====================================

def knowledge_node(state: AgentState):

    print("[Node] knowledge_node")

    return {
        "route": "knowledge",
        "result": (
            "识别为科研知识问题。\n"
            f"问题：{state['prepared_question']}\n"
            "后续将调用 ChromaDB 检索工具。"
        )
    }


# =====================================
# 5. Node 2B：普通问答处理
# =====================================

def general_node(state: AgentState):

    print("[Node] general_node")

    return {
        "route": "general",
        "result": (
            "识别为通用知识问题。\n"
            f"问题：{state['prepared_question']}\n"
            "后续将调用 DeepSeek 直接回答。"
        )
    }


# =====================================
# 6. 构建 Graph
# =====================================

builder = StateGraph(AgentState)

builder.add_node(
    "prepare_question",
    prepare_question
)

builder.add_node(
    "knowledge_node",
    knowledge_node
)

builder.add_node(
    "general_node",
    general_node
)


# =====================================
# 7. 配置 Edge
# =====================================

builder.add_edge(
    START,
    "prepare_question"
)

# 核心：条件边
builder.add_conditional_edges(
    "prepare_question",
    choose_route,
    {
        "knowledge": "knowledge_node",
        "general": "general_node"
    }
)

builder.add_edge("knowledge_node", END)
builder.add_edge("general_node", END)


# =====================================
# 8. 编译 Graph
# =====================================

graph = builder.compile()


# =====================================
# 9. 执行两组测试
# =====================================

if __name__ == "__main__":

    questions = [
        "根据我的知识库，LRFormer 的核心思想是什么？",
        "Python 的列表和元组有什么区别？"
    ]

    for question in questions:

        print("\n" + "=" * 55)
        print("用户问题：", question)

        final_state = graph.invoke({
            "question": question
        })

        print("\n========== 最终状态 ==========")
        print("执行路径：", final_state["route"])
        print("执行结果：")
        print(final_state["result"])
