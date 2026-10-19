
from typing import TypedDict, NotRequired

from langgraph.graph import (
    StateGraph,
    START,
    END
)


# =====================================
# 1. 定义 State（共享状态）
# =====================================

class AgentState(TypedDict):
    question: str
    prepared_question: NotRequired[str]
    result: NotRequired[str]


# =====================================
# 2. 定义第一个 Node
# =====================================

def prepare_question(state: AgentState):
    """清理用户输入的问题。"""

    print("\n[Node 1] 处理用户问题")

    question = state["question"].strip()

    if not question:
        raise ValueError("用户问题不能为空")

    return {
        "prepared_question": question
    }


# =====================================
# 3. 定义第二个 Node
# =====================================

def generate_result(state: AgentState):
    """生成演示结果，暂不调用大模型。"""

    print("[Node 2] 生成演示结果")

    question = state["prepared_question"]

    return {
        "result": (
            f"已接收到问题：{question}\n"
            "下一步将由 DeepSeek 处理该问题。"
        )
    }


# =====================================
# 4. 创建 StateGraph
# =====================================

builder = StateGraph(AgentState)

# 注册节点
builder.add_node(
    "prepare_question",
    prepare_question
)

builder.add_node(
    "generate_result",
    generate_result
)


# =====================================
# 5. 定义 Edge（执行顺序）
# =====================================

builder.add_edge(
    START,
    "prepare_question"
)

builder.add_edge(
    "prepare_question",
    "generate_result"
)

builder.add_edge(
    "generate_result",
    END
)


# =====================================
# 6. 编译 Graph
# =====================================

graph = builder.compile()


# =====================================
# 7. 执行 Graph
# =====================================

if __name__ == "__main__":

    initial_state = {
        "question": "  LRFormer 的核心思想是什么？  "
    }

    print("========== 初始 State ==========")
    print(initial_state)

    final_state = graph.invoke(initial_state)

    print("\n========== 最终 State ==========")

    for key, value in final_state.items():
        print(f"{key}: {value}")
