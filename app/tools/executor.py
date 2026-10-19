
"""
文件：app/tools/executor.py

用途：
    提供 Sweet Research Agent 的受控工具执行节点。

主要功能：
    1. 重新检查 Tool Policy。
    2. 校验每个工具调用对应的审批记录。
    3. 比较工具名称和参数指纹。
    4. 只将获得授权的调用提交给 ToolNode。
    5. 为拒绝的调用生成对应的 ToolMessage。

安全说明：
    本模块是当前 Agent 工作流的统一执行入口。

    真实高风险工具还需要额外的操作系统权限隔离、
    身份验证、幂等保护和审计记录。
"""

import json

from langchain_core.runnables import RunnableConfig

from app.observability.audit import write_audit_events
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt import ToolNode

from app.agents.state import ResearchAgentState
from app.agents.tool_policy import get_tool_policy
from app.workflows.tool_approval import (
    get_pending_calls,
    tool_fingerprint,
)
from app.tools.knowledge import search_knowledge_tool
from app.tools.simulated_write import simulate_save_note


# =====================================
# 1. 注册允许执行的工具
# =====================================

EXECUTABLE_TOOLS = [
    search_knowledge_tool,
    simulate_save_note,
]

TOOL_NAMES = {
    tool.name for tool in EXECUTABLE_TOOLS
}

# ToolNode 只拥有上面显式注册的工具。
# 模型不能仅通过提供任意名称就执行其他函数。
TOOL_NODE = ToolNode(EXECUTABLE_TOOLS)


# =====================================
# 2. Execution Guard
# =====================================

def check_execution_permission(
    call: dict,
    approval_records: dict
):
    """
    执行前重新验证工具调用。

    Returns:
        (allowed, reason)

    只有通过所有校验，才允许进入 ToolNode。
    """

    call_id = call["id"]
    tool_name = call["name"]

    # 第一关：实际执行器工具白名单
    if tool_name not in TOOL_NAMES:
        return False, "工具未注册到受控执行器"

    # 第二关：重新读取当前策略
    policy = get_tool_policy(tool_name)

    if policy == "deny":
        return False, "工具被策略禁止"

    # 第三关：必须存在审批节点产生的记录
    record = approval_records.get(call_id)

    if not isinstance(record, dict):
        return False, "缺少工具审批记录"

    # 第四关：审批记录与工具名称一致
    if record.get("name") != tool_name:
        return False, "审批工具名称不匹配"

    # 第五关：审批参数和当前执行参数一致
    expected_fingerprint = tool_fingerprint(call)

    if record.get("fingerprint") != expected_fingerprint:
        return False, "工具参数与审批记录不匹配"

    # 第六关：根据策略核验审批决定
    decision = record.get("decision")

    if policy == "auto" and decision == "auto":
        return True, "自动授权"

    if policy == "approve" and decision == "approve":
        return True, "人工已批准"

    return False, "操作未获得有效批准"


# =====================================
# 3. 生成拒绝执行的 ToolMessage
# =====================================

def rejected_tool_message(
    call: dict,
    reason: str
):
    """
    为没有执行的工具调用生成结果消息。

    即使操作被拒绝，也必须保留原 tool_call_id，
    使模型能够正确对应工具请求和执行结果。
    """

    content = json.dumps({
        "status": "rejected",
        "reason": reason,
        "tool_name": call["name"],
    }, ensure_ascii=False)

    return ToolMessage(
        content=content,
        name=call["name"],
        tool_call_id=call["id"],
        status="error",
    )


# =====================================
# 4. 受控工具执行节点
# =====================================

def controlled_tools_node(
    state: ResearchAgentState,
    config: RunnableConfig
):
    """
    执行经过策略和审批校验的工具调用。

    流程：
        1. 遍历所有 tool_calls。
        2. 区分允许与拒绝的调用。
        3. 仅执行允许的调用。
        4. 汇总所有 ToolMessage。
        5. 清理当前批次的审批记录。
    """

    print("\n[Executor] 开始执行前校验")

    calls = get_pending_calls(state)

    approval_records = state.get(
        "approval_records", {}
    )

    allowed_calls = []
    results_by_id = {}

    # =================================
    # 5. 为每个工具调用独立授权
    # =================================

    for call in calls:

        allowed, reason = check_execution_permission(
            call,
            approval_records,
        )

        if allowed:

            print(
                f"[Executor] 允许："
                f"{call['name']} ({call['id']})"
            )

            allowed_calls.append({
                "id": call["id"],
                "name": call["name"],
                "args": call["args"],
                "type": "tool_call",
            })

        else:

            print(
                f"[Executor] 拒绝："
                f"{call['name']} ({call['id']})"
                f"；原因：{reason}"
            )

            results_by_id[call["id"]] = (
                rejected_tool_message(call, reason)
            )

    # =================================
    # 6. 通过 ToolNode 执行允许的调用
    # =================================

    if allowed_calls:

        # 这里不是把完整的原始 tool_calls
        # 直接交给 ToolNode。
        #
        # 而是重新构造一个仅包含已授权调用的
        # AIMessage，避免被拒绝的调用被执行。
        filtered_message = AIMessage(
            content="",
            tool_calls=allowed_calls,
        )

        tool_result = TOOL_NODE.invoke({
            "messages": [filtered_message]
        })

        tool_messages = tool_result["messages"]

        expected_ids = {
            call["id"] for call in allowed_calls
        }

        # 校验执行器实际返回的工具调用 ID。
        # 每个获准调用必须对应恰好一个结果。
        returned_ids = [
            msg.tool_call_id for msg in tool_messages
        ]

        if (
            len(returned_ids) != len(expected_ids)
            or set(returned_ids) != expected_ids
        ):
            raise RuntimeError(
                "ToolNode 返回的调用结果不完整或重复"
            )

        for message in tool_messages:
            results_by_id[message.tool_call_id] = message

    # =================================
    # 7. 恢复原始工具调用顺序
    # =================================

    # 每个原始调用都必须对应一条 ToolMessage。
    # 即使有的工具被拒绝，也不能丢失其结果。
    messages = [
        results_by_id[call["id"]]
        for call in calls
    ]

    # =====================================
    # 为每个工具调用生成执行审计事件
    # =====================================

    authorized_ids = {
        call["id"]
        for call in allowed_calls
    }

    execution_events = []

    for call in calls:

        call_id = call["id"]

        # 没有获得授权的工具不会进入 ToolNode。
        if call_id not in authorized_ids:
            outcome = "blocked"

        else:
            result_message = results_by_id[call_id]

            # ToolNode 返回错误状态时单独记录。
            if getattr(
                result_message,
                "status",
                "success"
            ) == "error":
                outcome = "tool_error"
            else:
                outcome = "completed"

        execution_events.append({
            "tool_call_id": call_id,
            "tool_name": call["name"],
            "args_sha256": tool_fingerprint(call),
            "policy": get_tool_policy(call["name"]),
            "event_type": "execution",
            "outcome": outcome,
        })

    write_audit_events(
        config,
        execution_events
    )

    print(
        "[Audit] 执行事件记录数：",
        len(execution_events)
    )

    print(
        "[Executor] 本批次结果数：",
        len(messages)
    )

    return {
        "messages": messages,

        # 清除当前批次的授权记录，
        # 避免后续请求误用上一批次的决定。
        "approval_records": {},
    }
