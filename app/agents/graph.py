
"""
文件：app/agents/graph.py

用途：
    构建 Sweet Research Agent 的 LangGraph 主工作流。

主要功能：
    1. 使用 DeepSeek 生成回答或工具调用。
    2. 所有工具请求统一经过 Approval Node。
    3. 通过 Execution Guard 校验授权并执行工具。
    4. 支持 SQLite Checkpointer 中断与恢复。
    5. 可选启用对话摘要记忆。

执行流程：
    START
      -> agent
      -> approval（存在工具请求时）
      -> controlled_tools
      -> agent
      -> summarize / END

安全说明：
    本模块不允许工具调用绕过受控执行节点。
    当前写入工具为模拟工具，不操作真实文件。
"""

import os
from pathlib import Path
from collections import Counter
from app.observability.tracing import trace_node
from langchain_core.runnables import RunnableConfig

from app.observability.llm_metrics import invoke_with_metrics

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from langchain_core.messages import (
    HumanMessage,
    AIMessage,
    ToolMessage,
    SystemMessage,
    RemoveMessage,
)

from langgraph.graph import StateGraph, START, END

from app.agents.state import ResearchAgentState
from app.agents.prompts import SYSTEM_PROMPT, SUMMARY_PROMPT

from app.workflows.tool_approval import approval_node
from app.tools.executor import (
    EXECUTABLE_TOOLS,
    controlled_tools_node,
)


# =====================================
# 1. DeepSeek 配置
# =====================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")

llm = ChatOpenAI(
    model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.getenv(
        "DEEPSEEK_BASE_URL",
        "https://api.deepseek.com"
    ),
    timeout=60,
    max_retries=1,
    use_responses_api=False,
    extra_body={
        "thinking": {"type": "disabled"}
    },
)

# 模型可以知道有哪些工具，
# 但工具的实际执行权限由 Approval + Executor 决定。
llm_with_tools = llm.bind_tools(EXECUTABLE_TOOLS)


# =====================================
# 2. Agent Node
# =====================================

def agent_node(
    state: ResearchAgentState,
    config: RunnableConfig,
):
    """
    调用 DeepSeek。

    如果需要工具，DeepSeek 返回 AIMessage.tool_calls。
    这里不会直接执行任何工具。
    """

    messages = [
        SystemMessage(
            content=(
                SYSTEM_PROMPT
                + "\n\n补充安全规则："
                "simulate_save_note 只是模拟保存，"
                "不会真正写入科研知识库。"
                "工具被用户拒绝后，不得声称操作成功。"
                "不要要求用户绕过审批机制。"
            )
        )
    ]

    summary = state.get("summary", "")

    if summary:
        print("[Memory] 加载历史摘要")

        messages.append(
            SystemMessage(
                content=(
                    "以下是较早对话的摘要，仅用于理解上下文。\n"
                    "涉及私人科研事实时，仍需检索原始资料。\n\n"
                    + summary
                )
            )
        )

    messages.extend(state["messages"])

    response = invoke_with_metrics(
    model=llm_with_tools,
    messages=messages,
    config=config,
    call_kind="agent",
    )

    print(
        "[Agent] 请求工具数量：",
        len(response.tool_calls)
    )

    return {"messages": [response]}


# =====================================
# 3. 检查历史 ToolMessage 完整性
# =====================================

def tool_messages_complete(messages):
    """
    检查工具调用请求与结果是否一一对应。

    摘要压缩之前必须进行校验，
    防止删除了部分 Tool Calling 消息。
    """

    requests = Counter(
        call["id"]
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    )

    responses = Counter(
        message.tool_call_id
        for message in messages
        if isinstance(message, ToolMessage)
    )

    return requests == responses


# =====================================
# 4. 对话摘要 Node
# =====================================

def summarize_node(
    state: ResearchAgentState,
    config: RunnableConfig,
):
    """
    总结较早对话，保留最近一轮完整消息。

    摘要只用于上下文理解，
    不作为原始科研证据。
    """

    print("[Memory] 开始整理历史对话")

    messages = state["messages"]

    human_positions = [
        i for i, message in enumerate(messages)
        if isinstance(message, HumanMessage)
    ]

    if len(human_positions) < 2:
        return {}

    # 最近一轮从最后一条 HumanMessage 开始。
    # 这样可保留该轮完整工具调用关系。
    last_start = human_positions[-1]

    older = messages[:last_start]
    recent = messages[last_start:]

    if not tool_messages_complete(older):
        print("[Memory] 工具消息不完整，跳过摘要")
        return {}

    history = []

    for message in older:

        if isinstance(message, HumanMessage):
            history.append(f"用户：{message.content}")

        elif (
            isinstance(message, AIMessage)
            and not message.tool_calls
        ):
            history.append(f"助手：{message.content}")

    previous_summary = state.get("summary", "")

    try:
        response = invoke_with_metrics(
        model=llm,
        messages=[
            SystemMessage(content=SUMMARY_PROMPT),
            HumanMessage(
                content=(
                    f"已有摘要：\n{previous_summary or '无'}\n\n"
                    "新增历史：\n"
                    + "\n".join(history)
                )
            )
        ],
        config=config,
        call_kind="summary",
        )

        if not isinstance(response.content, str):
            raise ValueError("摘要返回内容不是字符串")

        new_summary = response.content.strip()

        if not new_summary:
            raise ValueError("摘要内容为空")

    except Exception as exc:
        print(
            "[Memory] 摘要失败，保留原始活动消息：",
            type(exc).__name__
        )
        return {}

    print("[Memory] 摘要更新成功")
    print("[Memory] 移除较早消息：", len(older))
    print("[Memory] 保留最近消息：", len(recent))

    return {
        "summary": new_summary,
        "messages": [
            RemoveMessage(id=message.id)
            for message in older
        ]
    }


# =====================================
# 5. 构建正式 Graph
# =====================================

def build_graph(
    checkpointer=None,
    enable_summarization=False
):
    """
    创建 Sweet Research Agent。

    Args:
        checkpointer:
            LangGraph 状态存储。
            启用人工审批时必须提供 Checkpointer。

        enable_summarization:
            是否启用历史对话自动摘要。

    Returns:
        编译后的 LangGraph。
    """

    builder = StateGraph(ResearchAgentState)

    # ---------------------------------
    # 注册节点
    # ---------------------------------

    builder.add_node(
        "agent",
        trace_node(
            "agent",
            agent_node,
            accepts_config=True,
        )
    )

    builder.add_node(
        "approval",
        trace_node(
            "approval",
            approval_node,
            accepts_config=True,
        )
    )

    builder.add_node(
        "controlled_tools",
        trace_node(
            "controlled_tools",
            controlled_tools_node,
            accepts_config=True,
        )
    )

    # ---------------------------------
    # 固定执行关系
    # ---------------------------------

    builder.add_edge(START, "agent")

    # 审批结束后，必须经过执行守卫。
    # 不能直接把工具请求交给普通 ToolNode。
    builder.add_edge(
        "approval",
        "controlled_tools"
    )

    # 工具执行完成后，再次交给 DeepSeek。
    builder.add_edge(
        "controlled_tools",
        "agent"
    )

    # ---------------------------------
    # 可选摘要节点
    # ---------------------------------

    if enable_summarization:
        builder.add_node(
            "summarize",
            trace_node(
                "summarize",
                summarize_node,
                accepts_config=True,
            )
        )

        builder.add_edge(
            "summarize",
            END
        )

    # ---------------------------------
    # Agent 的条件路由
    # ---------------------------------

    def route_after_agent(state: ResearchAgentState):
        """
        决定 Agent 下一步执行什么。

        优先级：
            1. 有工具调用 -> approval
            2. 无工具调用且需要摘要 -> summarize
            3. 否则 -> END
        """

        last = state["messages"][-1]

        if isinstance(last, AIMessage) and last.tool_calls:
            return "approval"

        if enable_summarization:

            turns = sum(
                isinstance(m, HumanMessage)
                for m in state["messages"]
            )

            if turns >= 3:
                return "summarize"

        return "end"

    routes = {
        "approval": "approval",
        "end": END,
    }

    if enable_summarization:
        routes["summarize"] = "summarize"

    builder.add_conditional_edges(
        "agent",
        route_after_agent,
        routes
    )

    return builder.compile(
        checkpointer=checkpointer
    )
