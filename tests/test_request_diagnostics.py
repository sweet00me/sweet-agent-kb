
"""
文件：tests/test_request_diagnostics.py

用途：
    验证 Request Journal 与 LangGraph 状态的诊断逻辑。

特点：
    使用模拟 Snapshot，不访问真实 SQLite。
    不调用 DeepSeek。
    不执行工具或修改工作流状态。
"""

from types import SimpleNamespace

from app.api.recovery import (
    get_checkpoint_status,
    diagnose_request,
)


def make_snapshot(
    *,
    values=None,
    next_nodes=(),
    interrupts=(),
):
    """构造用于测试的 LangGraph Snapshot。"""

    return SimpleNamespace(
        values=values or {},
        next=next_nodes,
        interrupts=interrupts,
    )


def make_record(
    status: str,
    response=None,
):
    """构造一条模拟请求记录。"""

    return {
        "request_id": "req-diagnostic-test-001",
        "thread_id": "research-diagnostic-001",
        "operation": "chat",
        "status": status,
        "response": response,
    }


def main():

    print("========== 1. 已保存结果 ==========")

    completed = diagnose_request(
        make_record(
            "completed",
            response={
                "status": "completed",
                "answer": "测试回答",
            },
        ),
        make_snapshot(
            values={"messages": ["已有消息"]},
        ),
    )

    assert completed["diagnosis"] == "result_saved"
    assert completed["has_saved_response"]
    assert not completed["automatic_replay_allowed"]

    print("PASS：已完成请求正确识别")

    print("\n========== 2. 未确认的请求 ==========")

    uncertain = diagnose_request(
        make_record("uncertain"),
        make_snapshot(
            values={"messages": ["已有消息"]},
        ),
    )

    assert uncertain["checkpoint_status"] == "idle"
    assert uncertain["diagnosis"] == (
        "requires_reconciliation"
    )
    assert not uncertain["automatic_replay_allowed"]

    print("PASS：空闲线程不被误判为请求完成")

    print("\n========== 3. 人工审批中断 ==========")

    interrupted = SimpleNamespace(
        value={
            "type": "tool_approval",
            "calls": [],
        }
    )

    snapshot = make_snapshot(
        values={"messages": ["工具调用"]},
        next_nodes=("approval",),
        interrupts=(interrupted,),
    )

    assert get_checkpoint_status(snapshot) == (
        "awaiting_approval"
    )

    pending = diagnose_request(
        make_record("uncertain"),
        snapshot,
    )

    assert pending["diagnosis"] == (
        "requires_reconciliation"
    )

    print("PASS：审批中断被识别，但未自动批准")

    print("\n========== 4. 待执行节点 ==========")

    unfinished = diagnose_request(
        make_record("running"),
        make_snapshot(
            values={"messages": ["消息"]},
            next_nodes=("controlled_tools",),
        ),
    )

    assert unfinished["checkpoint_status"] == (
        "unfinished"
    )
    assert not unfinished["automatic_replay_allowed"]

    print("PASS：未完成工作流禁止自动重放")

    print("\n========== 5. 历史审批结果 ==========")

    old_pending = diagnose_request(
        make_record(
            "pending_approval",
            response={
                "status": "pending_approval",
            },
        ),
        make_snapshot(
            values={"messages": ["已有消息"]},
        ),
    )

    assert old_pending["diagnosis"] == (
        "approval_response_saved"
    )
    assert old_pending["checkpoint_status"] == "idle"

    print("PASS：历史审批结果与当前线程状态分离")

    print("\n请求异常状态诊断测试全部通过")


if __name__ == "__main__":
    main()
