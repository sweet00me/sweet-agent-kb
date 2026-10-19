
"""
文件：tests/test_agent_behavior.py

用途：
    对 Sweet Research Agent 正式 Graph 进行离线行为评测。

主要功能：
    1. 模拟 DeepSeek 返回多个工具调用。
    2. 验证人工审批、中断与恢复。
    3. 验证 Execution Guard 阻止未授权工具。
    4. 验证 ToolMessage 与 tool_call_id 一一对应。
    5. 验证 Audit、Tracing、LLM Metrics 集成正常。

测试隔离：
    使用 Fake LLM 和 Fake ToolNode。
    Checkpointer 使用 InMemorySaver。
    观测数据写入临时 SQLite，测试后自动删除。

不会调用真实 API，也不会修改正式知识库。
"""

import json
import uuid

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from langchain_core.messages import (
    HumanMessage,
    AIMessage,
    ToolMessage,
)
from langgraph.types import Command
from langgraph.checkpoint.memory import InMemorySaver

import app.agents.graph as graph_module
import app.tools.executor as executor_module
import app.observability.audit as audit_module
import app.observability.tracing as tracing_module
import app.observability.llm_metrics as metrics_module


# =====================================
# 1. 固定的工具调用测试集
# =====================================

TEST_TOOL_CALLS = [
    {
        "id": "call_eval_001",
        "name": "search_knowledge_tool",
        "args": {
            "question": "RAG 与 Agent 的关系",
            "top_k": 1,
        },
        "type": "tool_call",
    },
    {
        "id": "call_eval_002",
        "name": "simulate_save_note",
        "args": {
            "title": "批准的测试笔记",
            "content": "这条模拟操作应该被允许",
        },
        "type": "tool_call",
    },
    {
        "id": "call_eval_003",
        "name": "simulate_save_note",
        "args": {
            "title": "拒绝的测试笔记",
            "content": "这条模拟操作不能执行",
        },
        "type": "tool_call",
    },
    {
        "id": "call_eval_004",
        "name": "unknown_tool",
        "args": {},
        "type": "tool_call",
    },
]


# =====================================
# 2. Fake DeepSeek
# =====================================

class FakeDeepSeek:
    """
    模拟两次模型调用。

    第一次：
        返回四个工具调用。

    第二次：
        读取工具执行结果，返回最终回答。

    不连接网络。
    """

    def __init__(self):
        self.call_count = 0

    def invoke(self, messages):

        self.call_count += 1

        if self.call_count == 1:

            return AIMessage(
                id="fake-llm-response-001",
                content="",
                tool_calls=TEST_TOOL_CALLS,
                response_metadata={
                    "model_name": "fake-evaluation-model"
                },
                usage_metadata={
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
            )

        if self.call_count == 2:

            # 第二次模型调用必须能够收到
            # 所有四个工具的结果，包括被拒绝的调用。
            tool_messages = [
                msg for msg in messages
                if isinstance(msg, ToolMessage)
            ]

            assert len(tool_messages) == 4

            return AIMessage(
                id="fake-llm-response-002",
                content="模拟模型已收到全部工具结果。",
                response_metadata={
                    "model_name": "fake-evaluation-model"
                },
                usage_metadata={
                    "input_tokens": 200,
                    "output_tokens": 30,
                    "total_tokens": 230,
                },
            )

        raise AssertionError(
            "Agent 发生了非预期的额外 LLM 调用"
        )


# =====================================
# 3. Fake ToolNode
# =====================================

class FakeToolNode:
    """
    记录实际被提交执行的工具调用。

    只返回模拟结果。
    通过 executed_ids 检查是否有未授权操作进入执行器。
    """

    def __init__(self):
        self.executed_ids = []

    def invoke(self, inputs):

        last_message = inputs["messages"][-1]
        calls = last_message.tool_calls

        results = []

        for call in calls:

            self.executed_ids.append(call["id"])

            if call["name"] == "search_knowledge_tool":

                data = {
                    "count": 1,
                    "matches": [{
                        "source": "notes/rag_intro.md",
                        "chunk_index": 0,
                        "content": "RAG 可作为 Agent 的检索工具",
                    }],
                }

            elif call["name"] == "simulate_save_note":

                data = {
                    "status": "simulated",
                    "message": "模拟保存完成",
                }

            else:
                raise AssertionError(
                    "未授权或未知工具进入 Fake ToolNode"
                )

            results.append(
                ToolMessage(
                    content=json.dumps(
                        data,
                        ensure_ascii=False
                    ),
                    name=call["name"],
                    tool_call_id=call["id"],
                    status="success",
                )
            )

        return {"messages": results}


# =====================================
# 4. 启动完整行为测试
# =====================================

def main():

    thread_id = (
        "eval-behavior-"
        + uuid.uuid4().hex[:12]
    )

    config = {
        "configurable": {
            "thread_id": thread_id
        },
        "recursion_limit": 12,
    }

    fake_llm = FakeDeepSeek()
    fake_tools = FakeToolNode()

    # 临时目录用于隔离本次测试的 Audit、
    # Tracing 和 Metrics，不污染正式统计。
    with TemporaryDirectory(
        prefix="sweet-agent-eval-"
    ) as temp_dir:

        temp_db = (
            Path(temp_dir) / "agent_audit.sqlite3"
        )

        with (
            patch.object(
                graph_module,
                "llm_with_tools",
                fake_llm
            ),
            patch.object(
                executor_module,
                "TOOL_NODE",
                fake_tools
            ),
            patch.object(
                audit_module,
                "AUDIT_DB_PATH",
                temp_db
            ),
            patch.object(
                tracing_module,
                "AUDIT_DB_PATH",
                temp_db
            ),
            patch.object(
                metrics_module,
                "AUDIT_DB_PATH",
                temp_db
            ),
        ):

            graph = graph_module.build_graph(
                checkpointer=InMemorySaver(),
                enable_summarization=False,
            )

            print("========== 1. 启动 Agent ==========")

            first = graph.invoke(
                {
                    "messages": [
                        HumanMessage(
                            content="执行四个测试工具调用"
                        )
                    ]
                },
                config=config
            )

            # E01：需要审批的调用应触发中断。
            assert "__interrupt__" in first

            pending = first["__interrupt__"][0].value

            pending_ids = {
                call["id"]
                for call in pending["calls"]
            }

            assert pending_ids == {
                "call_eval_002",
                "call_eval_003",
            }

            print("PASS E01：审批中断正确")

            # E02：在人工确认之前，不能执行工具。
            assert fake_tools.executed_ids == []

            print("PASS E02：暂停前未执行工具")

            snapshot = graph.get_state(config)

            assert snapshot.next == ("approval",)

            # =================================
            # 5. 提交人工决定并恢复
            # =================================

            print("\n========== 2. 人工审批 ==========")

            decisions = {
                "call_eval_002": "approve",
                "call_eval_003": "reject",
            }

            result = graph.invoke(
                Command(
                    resume={
                        "decisions": decisions
                    }
                ),
                config=config
            )

            # E03-E05：只有自动允许和明确批准的调用执行。
            assert fake_tools.executed_ids == [
                "call_eval_001",
                "call_eval_002",
            ]

            print("PASS E03-E05：独立审批及执行拦截正确")

            # =================================
            # 6. 检查消息链完整性
            # =================================

            tool_messages = [
                msg for msg in result["messages"]
                if isinstance(msg, ToolMessage)
            ]

            results = {
                msg.tool_call_id: json.loads(msg.content)
                for msg in tool_messages
            }

            assert len(tool_messages) == 4

            assert set(results) == {
                "call_eval_001",
                "call_eval_002",
                "call_eval_003",
                "call_eval_004",
            }

            assert results["call_eval_001"]["count"] == 1

            assert results["call_eval_002"]["status"] == (
                "simulated"
            )

            assert results["call_eval_003"]["status"] == (
                "rejected"
            )

            assert results["call_eval_004"]["status"] == (
                "rejected"
            )

            print("PASS E06：ToolMessage 一一对应")

            assert result["approval_records"] == {}

            print("PASS E07：审批记录已清理")

            assert fake_llm.call_count == 2
            assert isinstance(result["messages"][-1], AIMessage)
            assert not result["messages"][-1].tool_calls

            assert not graph.get_state(config).next

            # =================================
            # 7. 验证观测数据
            # =================================

            print("\n========== 3. 检查观测记录 ==========")

            audit_events = (
                audit_module.list_audit_events(thread_id)
            )

            traces = tracing_module.list_node_traces(
                thread_id
            )

            metrics = metrics_module.list_llm_metrics(
                thread_id
            )

            print("Audit 事件数：", len(audit_events))
            print("Tracing 节点数：", len(traces))
            print("LLM 调用数：", len(metrics))

            assert len(audit_events) == 8
            assert len(traces) == 5
            assert len(metrics) == 2

            assert [
                trace["status"] for trace in traces
            ] == [
                "completed",
                "interrupted",
                "completed",
                "completed",
                "completed",
            ]

            # Fake DeepSeek 返回的 Token 是测试数据，
            # 不代表真实 API 消耗。
            assert sum(
                m["total_tokens"] for m in metrics
            ) == 350

            print("PASS E08：Audit、Tracing、Metrics 正常")

    print("\n" + "=" * 52)
    print("Sweet Research Agent 行为评测全部通过")
    print("=" * 52)


if __name__ == "__main__":
    main()
