
import os
from pathlib import Path
from typing import TypedDict, NotRequired, Literal

from dotenv import load_dotenv
from openai import OpenAI
from langgraph.graph import StateGraph, START, END


# =====================================
# 1. 配置 DeepSeek
# =====================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.getenv(
        "DEEPSEEK_BASE_URL",
        "https://api.deepseek.com"
    ),
    timeout=60.0,
    max_retries=1
)

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")


# =====================================
# 2. 定义共享 State
# =====================================

class AgentState(TypedDict):
    question: str
    prepared_question: NotRequired[str]
    route: NotRequired[Literal["knowledge", "general"]]
    result: NotRequired[str]
    router_tokens: NotRequired[int]


# =====================================
# 3. Node 1：问题预处理
# =====================================

def prepare_question(state: AgentState):

    print("\n[Node 1] prepare_question")

    question = state["question"].strip()

    if not question:
        raise ValueError("问题不能为空")

    return {
        "prepared_question": question
    }


# =====================================
# 4. Node 2：DeepSeek 智能路由
# =====================================

def llm_router_node(state: AgentState):

    print("[Node 2] llm_router_node")

    system_prompt = """
你是私人科研 Agent 的任务路由器。

请根据用户问题，判断下一步应该执行哪条路径。

路径一：knowledge
当用户需要查询自己的私人资料时选择。
例如：
- 我的知识库中有什么信息
- 我保存的论文写了什么
- 我以前的实验结果是多少
- 我的训练日志、配置文件包含什么内容

路径二：general
当用户询问普通公开知识时选择。
例如：
- Python 列表和元组有什么区别
- 什么是语义分割
- LRFormer 的一般原理是什么

注意：
- 不能只根据是否出现专业术语判断。
- 必须区分私人数据查询和通用知识问答。
- 只允许输出 knowledge 或 general。
- 不要输出解释、标点或其他文字。
"""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": state["prepared_question"]
            }
        ],
        max_tokens=50,
        extra_body={
            "thinking": {"type": "disabled"}
        }
    )

    # 读取 DeepSeek 的分类结果
    raw_output = (
        response.choices[0].message.content or ""
    )

    route = raw_output.strip().lower()

    # 对模型输出进行校验
    if route not in ("knowledge", "general"):
        raise ValueError(
            f"DeepSeek 返回了无效路径：{raw_output!r}"
        )

    tokens = (
        response.usage.total_tokens
        if response.usage
        else 0
    )

    print("DeepSeek 选择：", route)
    print("本次路由 Token：", tokens)

    return {
        "route": route,
        "router_tokens": tokens
    }


# =====================================
# 5. Conditional Edge 路由函数
# =====================================

def choose_route(
    state: AgentState
) -> Literal["knowledge", "general"]:

    # 这里只读取 LLM Node 写入的结果
    return state["route"]


# =====================================
# 6. Node 3A：知识库路径
# =====================================

def knowledge_node(state: AgentState):

    print("[Node 3A] knowledge_node")

    return {
        "result": (
            "DeepSeek 判断这个问题需要私人资料。\n"
            "已进入知识库处理路径。\n"
            "本节暂不实际执行 ChromaDB 检索。"
        )
    }


# =====================================
# 7. Node 3B：通用问答路径
# =====================================

def general_node(state: AgentState):

    print("[Node 3B] general_node")

    return {
        "result": (
            "DeepSeek 判断这是通用知识问题。\n"
            "已进入普通问答路径。\n"
            "本节暂不实际生成问题答案。"
        )
    }


# =====================================
# 8. 构建 LangGraph
# =====================================

builder = StateGraph(AgentState)

builder.add_node(
    "prepare_question",
    prepare_question
)

builder.add_node(
    "llm_router_node",
    llm_router_node
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
# 9. 配置 Edge
# =====================================

builder.add_edge(
    START,
    "prepare_question"
)

builder.add_edge(
    "prepare_question",
    "llm_router_node"
)

builder.add_conditional_edges(
    "llm_router_node",
    choose_route,
    {
        "knowledge": "knowledge_node",
        "general": "general_node"
    }
)

builder.add_edge("knowledge_node", END)
builder.add_edge("general_node", END)


# =====================================
# 10. 编译
# =====================================

graph = builder.compile()


# =====================================
# 11. 三组测试
# =====================================

if __name__ == "__main__":

    questions = [
        "根据我的知识库，LRFormer 的核心思想是什么？",

        "Python 的列表和元组有什么区别？",

        "我之前在 ADE20K 上的 SD-LoRA 实验最终 mIoU 是多少？"
    ]

    for question in questions:

        print("\n" + "=" * 60)
        print("用户问题：", question)

        final_state = graph.invoke({
            "question": question
        })

        print("\n========== 最终 State ==========")
        print("路由结果：", final_state["route"])
        print("Token：", final_state["router_tokens"])
        print("执行结果：")
        print(final_state["result"])
