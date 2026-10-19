
"""
文件：tests/test_node_tracing.py

用途：
    验证 Sweet Research Agent 的节点追踪功能。

测试内容：
    1. 正常节点能够记录 completed。
    2. interrupt() 能够记录 interrupted。
    3. 恢复后能够记录新的 completed。
    4. 节点异常能够记录 error。
    5. 每次节点执行具有独立 span_id。

安全说明：
    不调用 DeepSeek 或 Embedding API。
    不修改知识库。
    只向审计数据库写入测试轨迹。
"""

import uuid

from typing import TypedDict, NotRequired

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import InMemorySaver

from app.observability.tracing import (
    trace_node,
    list_node_traces,
)


# =====================================
# 1. 测试 State
# =====================================

class DemoState(TypedDict):
    number: int
    approved: NotRequired[bool]


# =====================================
# 2. 三个测试节点
# =====================================

def prepare_node(state: DemoState):
    """先更新数值，验证普通节点。"""

    return {"number": state["number"] + 1}


def approval_node(state: DemoState):
    """暂停并等待人工确认。"""

    approved = interrupt({
        "message": "是否继续测试？"
    })

    return {"approved": approved}


def finish_node(state: DemoState):
    """批准后进行简单的数值计算。"""

    if state["approved"]:
        return {"number": state["number"] + 10}

    return {"number": state["number"]}


# =====================================
# 3. 构建测试 Graph
# =====================================

builder = StateGraph(DemoState)

builder.add_node(
    "prepare",
    trace_node("prepare", prepare_node)
)

builder.add_node(
    "approval",
    trace_node("approval", approval_node)
)

builder.add_node(
    "finish",
    trace_node("finish", finish_node)
)

builder.add_edge(START, "prepare")
builder.add_edge("prepare", "approval")
builder.add_edge("approval", "finish")
builder.add_edge("finish", END)

graph = builder.compile(
    checkpointer=InMemorySaver()
)


# 为每次测试生成新线程，避免历史轨迹干扰。
thread_id = f"trace-test-{uuid.uuid4().hex[:12]}"

config = {
    "configurable": {
        "thread_id": thread_id
    }
}


# =====================================
# 4. 测试正常执行与 interrupt
# =====================================

print("========== 1. 启动测试 ==========")

result = graph.invoke(
    {"number": 1},
    config=config
)

assert "__interrupt__" in result

snapshot = graph.get_state(config)

assert snapshot.interrupts
assert snapshot.next == ("approval",)

print("PASS：Graph 已暂停")


# =====================================
# 5. 模拟人工恢复
# =====================================

print("\n========== 2. 恢复执行 ==========")

result = graph.invoke(
    Command(resume=True),
    config=config
)

assert result["number"] == 12
assert result["approved"] is True

print("PASS：Graph 已完成")


# =====================================
# 6. 查看节点状态
# =====================================

print("\n========== 3. 检查节点轨迹 ==========")

traces = list_node_traces(thread_id)

for item in traces:
    print(
        item["node_name"],
        "->",
        item["status"],
        "|",
        item["duration_ms"],
        "ms"
    )

statuses = [
    item["status"]
    for item in traces
]

assert statuses == [
    "completed",
    "interrupted",
    "completed",
    "completed",
]

assert len({
    item["span_id"]
    for item in traces
}) == 4

print("PASS：中断和恢复轨迹正确")


# =====================================
# 7. 验证异常记录
# =====================================

def failing_node(state: DemoState):
    """故意抛出异常，验证追踪模块。"""

    raise ValueError("模拟节点错误")


error_builder = StateGraph(DemoState)

error_builder.add_node(
    "failing",
    trace_node("failing", failing_node)
)

error_builder.add_edge(START, "failing")
error_builder.add_edge("failing", END)

error_graph = error_builder.compile()

error_thread_id = (
    f"trace-error-{uuid.uuid4().hex[:12]}"
)

error_config = {
    "configurable": {
        "thread_id": error_thread_id
    }
}

print("\n========== 4. 测试异常追踪 ==========")

try:
    error_graph.invoke(
        {"number": 0},
        config=error_config
    )

except ValueError:
    print("PASS：节点原始异常正常传播")

else:
    raise AssertionError("异常没有正确传播")

error_traces = list_node_traces(error_thread_id)

assert len(error_traces) == 1
assert error_traces[0]["status"] == "error"
assert error_traces[0]["error_type"] == "ValueError"

print("PASS：错误轨迹已持久化")
print("\n节点 Tracing 测试全部通过")
