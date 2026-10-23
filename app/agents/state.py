"""
文件：app/agents/state.py

用途：
    定义 Sweet Research Agent 的共享状态。

主要字段：
    messages：
        对话消息及工具调用结果。

    summary：
        较早对话的压缩摘要。

    approval_records：
        当前一批工具调用的审批记录。
        用于执行前的二次授权校验。
"""

from typing import NotRequired

from langgraph.graph import MessagesState


class ResearchAgentState(MessagesState):
    """科研 Agent 的共享状态。"""

    summary: NotRequired[str]

    citation_report: NotRequired[dict | None]

    citation_replan: NotRequired[dict]

    approval_records: NotRequired[
    dict[str, dict[str, str]]
    ]
