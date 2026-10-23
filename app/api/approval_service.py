
"""
文件：app/api/approval_service.py

用途：
    校验 Web UI 提交的工具审批请求。

主要功能：
    1. 检查当前线程是否真的处于人工审批状态。
    2. 验证中断类型和待审批工具调用。
    3. 核对 tool_call_id、工具名称和参数指纹。
    4. 拒绝过期、缺失、额外或不合法的审批决定。

安全说明：
    不信任客户端提交的工具参数或审批状态。
    真实待审批调用以 LangGraph Checkpoint 为准。

    本模块只负责校验，不执行工具。
"""


from langchain_core.messages import AIMessage

from app.agents.tool_policy import classify_tool_calls
from app.workflows.tool_approval import tool_fingerprint


class ApprovalValidationError(Exception):
    """审批请求与当前工作流状态不一致。"""


def validate_review_request(
    snapshot,
    decisions: dict[str, str],
    expected_fingerprints: dict[str, str],
) -> dict:
    """
    校验一次 HTTP 工具审批。

    Returns:
        可直接传入 Command(resume=...) 的恢复数据。

    校验原则：
        - 只支持一个统一 Approval Node 中断；
        - 可以包含多个待审批工具；
        - 每个待审批工具都必须明确批准或拒绝；
        - 不能审批其他批次或已结束的调用。
    """

    # =====================================
    # 1. 检查当前是否处于审批暂停状态
    # =====================================

    interrupts = snapshot.interrupts

    if len(interrupts) != 1:
        raise ApprovalValidationError(
            "当前没有可处理的单一工具审批请求"
        )

    interrupt_data = interrupts[0].value

    if (
        not isinstance(interrupt_data, dict)
        or interrupt_data.get("type") != "tool_approval"
    ):
        raise ApprovalValidationError(
            "当前中断不是支持的工具审批类型"
        )

    pending_calls = interrupt_data.get("calls")

    if not isinstance(pending_calls, list) or not pending_calls:
        raise ApprovalValidationError(
            "当前审批请求没有有效的工具调用"
        )

    # =====================================
    # 2. 重新读取模型原始工具请求
    # =====================================

    messages = snapshot.values.get("messages", [])

    if not messages:
        raise ApprovalValidationError(
            "无法找到原始工具调用消息"
        )

    last_message = messages[-1]

    if (
        not isinstance(last_message, AIMessage)
        or not last_message.tool_calls
    ):
        raise ApprovalValidationError(
            "最新消息不是待执行的 AI 工具调用"
        )

    try:
        original_calls = classify_tool_calls(
            last_message.tool_calls
        )

    except (ValueError, KeyError, TypeError) as exc:
        raise ApprovalValidationError(
            "原始工具调用数据无效"
        ) from exc

    # 只有策略为 approve 的工具需要人工决定。
    review_calls = {
        call["id"]: call
        for call in original_calls
        if call["policy"] == "approve"
    }

    # =====================================
    # 3. 核对暂停信息与原始工具调用
    # =====================================

    pending_by_id = {}

    for item in pending_calls:

        if not isinstance(item, dict):
            raise ApprovalValidationError(
                "暂停请求中的工具数据无效"
            )

        call_id = item.get("id")

        if (
            not isinstance(call_id, str)
            or call_id in pending_by_id
        ):
            raise ApprovalValidationError(
                "暂停请求包含无效或重复的调用 ID"
            )

        pending_by_id[call_id] = item

    expected_ids = set(review_calls)

    if set(pending_by_id) != expected_ids:
        raise ApprovalValidationError(
            "待审批工具与原始模型调用不一致"
        )

    for call_id, call in review_calls.items():

        saved = pending_by_id[call_id]

        actual_fingerprint = tool_fingerprint(call)

        if (
            saved.get("name") != call["name"]
            or saved.get("args") != call["args"]
            or saved.get("fingerprint") != actual_fingerprint
        ):
            raise ApprovalValidationError(
                "待审批工具参数或指纹发生变化"
            )

    # =====================================
    # 4. 检查 HTTP 提交的决定
    # =====================================

    if set(decisions) != expected_ids:
        raise ApprovalValidationError(
            "必须逐项处理当前所有待审批工具"
        )

    if set(expected_fingerprints) != expected_ids:
        raise ApprovalValidationError(
            "请求中的工具参数指纹不完整"
        )

    for call_id, decision in decisions.items():

        if decision not in ("approve", "reject"):
            raise ApprovalValidationError(
                "审批决定只能是 approve 或 reject"
            )

        current_fingerprint = tool_fingerprint(
            review_calls[call_id]
        )

        if (
            expected_fingerprints[call_id]
            != current_fingerprint
        ):
            raise ApprovalValidationError(
                "审批页面已经过期或参数不匹配"
            )

    # =====================================
    # 5. 返回 LangGraph 所需的恢复数据
    # =====================================

    # 不把客户端的工具名称或参数传入 Graph。
    # 只提交针对已有 call_id 的审批决定。
    return {
        "decisions": dict(decisions)
    }
