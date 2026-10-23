
"""
文件：app/api/recovery.py

用途：
    为 Sweet Research Agent 提供请求异常状态诊断。

主要功能：
    1. 判断 LangGraph 当前线程的执行状态。
    2. 对比 Request Journal 中的请求记录。
    3. 区分已保存结果、等待审批和无法确认的状态。
    4. 提供前端可以理解的诊断建议。

安全设计：
    本模块只读取状态，不修改任何数据库。
    不调用 DeepSeek，不恢复 Graph，不执行工具。
    不根据线程最后一条回答推断某个历史请求已完成。
"""


def get_checkpoint_status(snapshot) -> str:
    """
    获取线程当前的 LangGraph 状态。

    返回值：
        not_checked       未读取 Checkpoint
        awaiting_approval 正在等待工具审批
        interrupted       其他类型的中断
        unfinished        工作流存在待执行节点
        idle              工作流当前没有待执行节点
        no_checkpoint     没有找到有效工作流状态

    注意：
        idle 表示线程当前空闲，
        并不证明某个历史 request_id 已完成。
    """

    if snapshot is None:
        return "not_checked"

    interrupts = getattr(
        snapshot, "interrupts", ()
    ) or ()

    if interrupts:

        if len(interrupts) == 1:

            payload = getattr(
                interrupts[0], "value", None
            )

            if (
                isinstance(payload, dict)
                and payload.get("type")
                == "tool_approval"
            ):
                return "awaiting_approval"

        return "interrupted"

    if getattr(snapshot, "next", ()):
        return "unfinished"

    values = getattr(snapshot, "values", None)

    if values:
        return "idle"

    return "no_checkpoint"


def diagnose_request(
    record: dict,
    snapshot=None,
) -> dict:
    """
    对比 Request Journal 与当前 Checkpoint。

    关键：
        只给出诊断结论，不自动改变请求状态。

    其中 completed + 已保存响应是明确的结果。

    running / accepted / uncertain 即使遇到空闲
    Checkpoint，也不能直接认定该请求已完成。
    """

    if not isinstance(record, dict):
        raise ValueError("请求记录无效")

    journal_status = record["status"]

    saved_response = record.get("response")

    has_saved_response = isinstance(
        saved_response, dict
    )

    checkpoint_status = get_checkpoint_status(
        snapshot
    )

    if (
        journal_status == "completed"
        and has_saved_response
    ):
        diagnosis = "result_saved"
        action = "read_saved_response"

    elif (
        journal_status == "pending_approval"
        and has_saved_response
    ):
        # 原请求曾进入待审批状态。
        # 之后可能已经通过另一条 review 请求完成。
        diagnosis = "approval_response_saved"
        action = "check_current_thread_status"

    elif journal_status in (
        "accepted",
        "running",
        "uncertain",
    ):
        # 不把线程当前状态当成该请求的完成证明。
        diagnosis = "requires_reconciliation"
        action = "inspect_checkpoint_history_and_audit"

    else:
        diagnosis = "inconsistent_record"
        action = "manual_inspection"

    return {
        "request_id": record["request_id"],
        "thread_id": record["thread_id"],
        "operation": record["operation"],
        "journal_status": journal_status,
        "checkpoint_status": checkpoint_status,
        "has_saved_response": has_saved_response,
        "diagnosis": diagnosis,
        "recommended_action": action,

        # 本接口从不授权自动重新执行。
        "automatic_replay_allowed": False,
        # 只读恢复已经持久化的终止响应，不通过再次 POST 审批来取结果。
        "response":saved_response if journal_status in {"completed","pending_approval"} and has_saved_response else None,
    }
