
"""
文件：app/workflows/tool_approval.py

用途：
    为 Sweet Research Agent 提供统一工具审批节点。

主要功能：
    1. 读取 AIMessage 中的 tool_calls。
    2. 根据 Tool Policy 判断是否需要人工审批。
    3. 对需要审批的调用执行 interrupt()。
    4. 将审批决定绑定工具 ID、名称及参数。
    5. 返回审批记录，供 Execution Guard 校验。

安全说明：
    本模块不执行任何工具。
    不修改文件，也不写入 ChromaDB。
"""

import json
import hashlib
from langchain_core.runnables import RunnableConfig

from app.observability.audit import write_audit_events

from langchain_core.messages import AIMessage
from langgraph.types import interrupt

from app.agents.tool_policy import classify_tool_calls
from app.agents.state import ResearchAgentState


# =====================================
# 1. 生成工具参数指纹
# =====================================

def tool_fingerprint(call: dict) -> str:
    """
    根据工具名称和参数生成 SHA256 摘要。

    审批与执行时使用相同的计算方式，
    用于检测待执行调用是否与审批对象一致。

    注意：
        SHA256 在这里用于一致性检查，
        不是可防止数据库或内存被篡改的数字签名。
    """

    payload = {
        "name": call["name"],
        "args": call["args"],
    }

    # 规范化 JSON，避免字典键顺序变化
    # 导致相同参数生成不同的摘要。
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )

    return hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()


# =====================================
# 2. 读取当前工具调用
# =====================================

def get_pending_calls(state: ResearchAgentState):
    """
    获取最新 AIMessage 的工具调用列表。

    必须来自 AIMessage，而不是用户普通消息。
    """

    messages = state["messages"]

    if not messages:
        raise ValueError("当前 State 中没有消息")

    last_message = messages[-1]

    if not isinstance(last_message, AIMessage):
        raise ValueError("最后一条消息不是 AIMessage")

    if not last_message.tool_calls:
        raise ValueError("当前没有工具调用请求")

    return classify_tool_calls(
        last_message.tool_calls
    )


# =====================================
# 3. 统一审批节点
# =====================================

def approval_node(
    state: ResearchAgentState,
    config: RunnableConfig
):
    """
    根据工具策略进行统一审批。

    执行逻辑：
        auto：
            标记为自动允许。

        approve：
            使用 interrupt() 等待人工决定。

        deny：
            直接记录禁止执行。

    注意：
        interrupt() 恢复时当前节点会重新执行。
        因此本节点不能执行真实工具操作。
    """

    print("\n[Approval] 开始审核工具调用")

    calls = get_pending_calls(state)

    # 只有 approve 策略需要人工确认。
    review_calls = [
        call for call in calls
        if call["policy"] == "approve"
    ]

    decisions = {}

    # =================================
    # 4. 请求人工确认
    # =================================

    if review_calls:

        print(
            "[Approval] 需要人工审批：",
            len(review_calls)
        )

        # 一个 interrupt 可以提交一批审批请求。
        # 每个工具调用由自己的 call_id 唯一标识。
        response = interrupt({
            "type": "tool_approval",
            "message": "请审核以下工具调用",
            "calls": [
                {
                    "id": call["id"],
                    "name": call["name"],
                    "args": call["args"],
                    "fingerprint": tool_fingerprint(call),
                }
                for call in review_calls
            ],
        })

        if not isinstance(response, dict):
            raise ValueError("审批回复必须是字典")

        decisions = response.get("decisions")

        if not isinstance(decisions, dict):
            raise ValueError(
                "审批回复缺少 decisions 字典"
            )

        valid_ids = {
            call["id"] for call in review_calls
        }

        if not set(decisions).issubset(valid_ids):
            raise ValueError(
                "审批回复包含未知的工具调用 ID"
            )

        # 明确拒绝不合法的决定值。
        # 遗漏的调用在下面默认视为 reject。
        if any(
            decision not in ("approve", "reject")
            for decision in decisions.values()
        ):
            raise ValueError("审批决定必须是 approve 或 reject")

    # =================================
    # 5. 生成审批记录
    # =================================

    approval_records = {}

    for call in calls:

        policy = call["policy"]

        if policy == "auto":
            decision = "auto"

        elif policy == "approve":
            # 没有明确批准的调用，默认拒绝。
            decision = decisions.get(
                call["id"],
                "reject"
            )

        else:
            decision = "deny"

        # 审批记录绑定当前工具的名称和参数指纹。
        # Execution Guard 会重新校验。
        approval_records[call["id"]] = {
            "name": call["name"],
            "fingerprint": tool_fingerprint(call),
            "decision": decision,
        }

        print(
            f"[Approval] {call['id']} "
            f"{call['name']} -> {decision}"
        )


    # =====================================
    # 审批结果写入持久化审计日志
    # =====================================

    audit_events = []

    for call in calls:

        audit_events.append({
            "tool_call_id": call["id"],
            "tool_name": call["name"],
            "args_sha256": tool_fingerprint(call),
            "policy": call["policy"],
            "event_type": "decision",
            "outcome": approval_records[
                call["id"]
            ]["decision"],
        })

    write_audit_events(
        config,
        audit_events
    )

    print(
        "[Audit] 审批事件记录数：",
        len(audit_events)
    )

    return {
        "approval_records": approval_records
    }
