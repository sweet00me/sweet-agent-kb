
"""
文件：tests/test_tool_approval.py

用途：
    验证统一 Approval Node 与 Execution Guard。

测试内容：
    1. 只读工具自动允许。
    2. 第一个模拟写入工具由人工批准。
    3. 第二个模拟写入工具由人工拒绝。
    4. 未知工具由 Tool Policy 自动禁止。
    5. 验证每个 Tool Call 都有对应 ToolMessage。

说明：
    本测试使用 InMemorySaver，不修改正式 SQLite。

    会调用一次知识检索工具及百炼 Embedding API，
    但不会调用 DeepSeek，也不会写入真实笔记。
"""

import json

from langchain_core.messages import (
    HumanMessage,
    AIMessage,
    ToolMessage,
)

from langgraph.graph import (
    StateGraph,
    START,
    END,
)

from langgraph.types import Command
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.state import ResearchAgentState
from app.workflows.tool_approval import approval_node
from app.tools.executor import controlled_tools_node


# =====================================
# 1. 构建独立测试 Graph
# =====================================

builder = StateGraph(ResearchAgentState)

builder.add_node("approval", approval_node)
builder.add_node("controlled_tools", controlled_tools_node)

builder.add_edge(START, "approval")
builder.add_edge("approval", "controlled_tools")
builder.add_edge("controlled_tools", END)

graph = builder.compile(
    checkpointer=InMemorySaver()
)


# =====================================
# 2. 构造四个工具调用
# =====================================

tool_calls = [
    {
        "id": "call_001",
        "name": "search_knowledge_tool",
        "args": {
            "question": "RAG 与 Agent 有什么关系？",
            "top_k": 1,
        },
        "type": "tool_call",
    },
    {
        "id": "call_002",
        "name": "simulate_save_note",
        "args": {
            "title": "批准的笔记",
            "content": "测试人工批准后的执行流程",
        },
        "type": "tool_call",
    },
    {
        "id": "call_003",
        "name": "simulate_save_note",
        "args": {
            "title": "拒绝的笔记",
            "content": "这条模拟操作应被拒绝",
        },
        "type": "tool_call",
    },
    {
        "id": "call_004",
        "name": "unknown_tool",
        "args": {},
        "type": "tool_call",
    },
]


config = {
    "configurable": {
        "thread_id": "approval-guard-test-001"
    }
}


# =====================================
# 3. 启动审批
# =====================================

print("========== 启动审批测试 ==========")

result = graph.invoke(
    {
        "messages": [
            HumanMessage(
                content="测试四个工具调用"
            ),
            AIMessage(
                content="",
                tool_calls=tool_calls,
            ),
        ]
    },
    config=config,
)

assert "__interrupt__" in result

print("\n========== 等待审批 ==========")

for item in result["__interrupt__"]:
    print(json.dumps(
        item.value,
        ensure_ascii=False,
        indent=2,
    ))

# 只有 call_002 和 call_003 需要人工审批。
pending = result["__interrupt__"][0].value["calls"]

assert {call["id"] for call in pending} == {
    "call_002",
    "call_003",
}


# =====================================
# 4. 人工提交不同的审批结果
# =====================================

print("\n========== 模拟人工审批 ==========")

decisions = {
    "call_002": "approve",
    "call_003": "reject",
}

print(decisions)

result = graph.invoke(
    Command(
        resume={
            "decisions": decisions
        }
    ),
    config=config,
)


# =====================================
# 5. 检查工具结果
# =====================================

messages = result["messages"]

tool_messages = [
    message
    for message in messages
    if isinstance(message, ToolMessage)
]

print("\n========== 工具执行结果 ==========")

for message in tool_messages:

    print("\nTool Call ID：", message.tool_call_id)
    print("工具名称：", message.name)
    print("执行结果：", str(message.content)[:250])

assert len(tool_messages) == 4

results = {
    message.tool_call_id: message
    for message in tool_messages
}

assert set(results) == {
    "call_001",
    "call_002",
    "call_003",
    "call_004",
}

assert json.loads(
    results["call_002"].content
)["status"] == "simulated"

assert json.loads(
    results["call_003"].content
)["status"] == "rejected"

assert json.loads(
    results["call_004"].content
)["status"] == "rejected"

# 验证旧审批记录没有被保留。
assert result["approval_records"] == {}

print("\n全部审批与执行校验通过")
