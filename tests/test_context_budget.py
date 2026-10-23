"""上下文预算回归：本地官方词表，只有云端摘要/回答使用替身。"""
import os
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.memory.context_budget import (
    ContextBudgetError, ContextSettings, TokenCounter, get_token_counter,
    prepare_context,
)
from tests.test_citation_api import CitationApiFixture


def turn(number, text):
    return [HumanMessage(content=text, id=f"h-{number}"),
            AIMessage(content="已记录。", id=f"a-{number}")]


def render(messages, summary):
    prefix = [SystemMessage(content="保留用户约束。")]
    if summary:
        prefix.append(SystemMessage(content="较早历史摘要：" + summary))
    return prefix + list(messages)


class ContextBudgetTests(unittest.TestCase):
    def setUp(self):
        self.counter = get_token_counter()
        self.settings = ContextSettings(trigger_tokens=8000, target_tokens=5000,
                                        summary_tokens=512, summary_batch_tokens=4096)
        self.summary_inputs = []

    def summarize(self, messages):
        self.summary_inputs.append(messages)
        return "用户约束：保留原始记录，禁止删除文件。已完成先前讨论。"

    def prepare(self, messages, summary="", **kwargs):
        return prepare_context(messages, summary, render_prompt=render,
                               tools=kwargs.pop("tools", []), settings=kwargs.pop("settings", self.settings),
                               counter=self.counter, summarize=kwargs.pop("summarize", self.summarize), **kwargs)

    def test_official_tokenizer_handles_chinese_english_and_mixed_text(self):
        self.assertEqual(self.counter.text("Hello world"), 2)
        self.assertEqual(self.counter.text("你好，世界！"), 4)
        self.assertEqual(self.counter.text("中文和 English mixed 123"), 6)

    def test_many_small_turns_do_not_trigger_summary(self):
        messages = sum((turn(i, "继续讨论上下文。") for i in range(20)), [])
        result = self.prepare(messages)
        self.assertEqual(result.messages, messages)
        self.assertEqual(result.removed_ids, [])
        self.assertEqual(self.summary_inputs, [])

    def test_large_history_compacts_to_target_and_keeps_recent_verbatim(self):
        messages = sum((turn(i, "alpha beta gamma delta " * 500) for i in range(8)), [])
        result = self.prepare(messages)
        self.assertGreaterEqual(result.before_tokens, 8000)
        self.assertLessEqual(result.after_tokens, 5000)
        self.assertTrue(result.target_reached)
        self.assertTrue(result.removed_ids)
        self.assertEqual(result.messages[-2:], messages[-2:])
        self.assertIn("禁止删除文件", result.summary)
        self.assertTrue(self.summary_inputs)
        self.assertEqual(len(messages), 16, "原始输入不能被就地删除")
        for request in self.summary_inputs:
            self.assertLessEqual(self.counter.messages(request), 4096)

    def test_next_small_turn_after_compaction_is_not_summarized_again(self):
        messages = sum((turn(i, "alpha beta gamma delta " * 500) for i in range(8)), [])
        first = self.prepare(messages)
        count = len(self.summary_inputs)
        second = self.prepare(first.messages + turn(10, "再确认一下。"), first.summary)
        self.assertEqual(second.removed_ids, [])
        self.assertEqual(len(self.summary_inputs), count)

    def test_current_turn_above_target_is_preserved_below_trigger(self):
        messages = turn(0, "alpha " * 3000) + turn(1, "beta " * 6000)
        result = self.prepare(messages)
        self.assertGreater(result.after_tokens, self.settings.target_tokens)
        self.assertLess(result.after_tokens, self.settings.trigger_tokens)
        self.assertFalse(result.target_reached)
        self.assertEqual(result.messages, messages[-2:])

    def test_trigger_is_inclusive_at_exact_measured_boundary(self):
        messages = sum((turn(i, "alpha beta gamma delta " * 500) for i in range(4)), [])
        tokens = self.counter.messages(render(messages, ""))
        below = self.prepare(messages, settings=replace(self.settings, trigger_tokens=tokens + 1))
        self.assertEqual(below.removed_ids, [])
        for trigger in (tokens, tokens - 1):
            with self.subTest(trigger=trigger):
                result = self.prepare(messages, settings=replace(self.settings, trigger_tokens=trigger))
                self.assertTrue(result.removed_ids)
                self.assertLessEqual(result.after_tokens, self.settings.target_tokens)

    def test_tool_definitions_and_results_both_count_toward_trigger(self):
        messages = turn(0, "alpha beta " * 1200) + turn(1, "当前问题")
        base = self.prepare(messages)
        self.assertEqual(base.removed_ids, [])
        schema = [{"type": "function", "function": {"name": "lookup", "description": "word " * 9000}}]
        with self.assertRaises(ContextBudgetError):
            self.prepare(messages, tools=schema)
        long_tool = ToolMessage(content="evidence " * 9000, id="tool-0", tool_call_id="lookup")
        active = [HumanMessage(content="当前问题", id="current"),
                  AIMessage(content="", id="call", tool_calls=[
                      {"id": "lookup", "name": "lookup", "args": {}}]), long_tool]
        with self.assertRaises(ContextBudgetError):
            self.prepare(active)

    def test_compaction_preserves_complete_tool_call_pairs(self):
        history = []
        for i in range(5):
            history += [HumanMessage(content="alpha " * 1600, id=f"h{i}"),
                        AIMessage(content="", id=f"call{i}", tool_calls=[
                            {"id": f"lookup{i}", "name": "lookup", "args": {}}]),
                        ToolMessage(content="文档原文。", id=f"result{i}", tool_call_id=f"lookup{i}"),
                        AIMessage(content="结论。", id=f"answer{i}")]
        current = [HumanMessage(content="最新问题", id="new-human"),
                   AIMessage(content="", id="new-call", tool_calls=[
                       {"id": "new-lookup", "name": "lookup", "args": {}}]),
                   ToolMessage(content="本轮证据", id="new-result", tool_call_id="new-lookup")]
        result = self.prepare(history + current)
        self.assertEqual(result.messages[-3:], current)
        requests = {call["id"] for m in result.messages if isinstance(m, AIMessage) for call in m.tool_calls}
        responses = {m.tool_call_id for m in result.messages if isinstance(m, ToolMessage)}
        self.assertEqual(requests, responses)

    def test_incomplete_tool_history_is_never_compacted(self):
        messages = sum((turn(i, "alpha beta " * 1200) for i in range(5)), [])
        messages.insert(1, AIMessage(content="", id="missing-result", tool_calls=[
            {"id": "incomplete", "name": "lookup", "args": {}}]))
        with self.assertRaisesRegex(ContextBudgetError, "incomplete_tool_history"):
            self.prepare(messages)
        self.assertEqual(self.summary_inputs, [])

    def test_summary_failure_preserves_original_messages_and_fails_closed(self):
        messages = sum((turn(i, "alpha beta " * 1200) for i in range(5)), [])
        original = [m.model_dump() for m in messages]
        def fail(_):
            raise TimeoutError("模拟摘要超时")
        with self.assertRaises(ContextBudgetError):
            self.prepare(messages, summarize=fail)
        self.assertEqual([m.model_dump() for m in messages], original)

    def test_empty_and_oversized_summary_are_rejected(self):
        messages = sum((turn(i, "alpha beta " * 1200) for i in range(5)), [])
        for answer in ("", "overflow " * 2000):
            with self.subTest(answer_length=len(answer)), self.assertRaises(ContextBudgetError):
                self.prepare(messages, summarize=lambda _: answer)

    def test_oversized_current_turn_is_not_silently_cut(self):
        messages = turn(0, "旧消息") + [HumanMessage(content="alpha beta " * 6000, id="latest")]
        with self.assertRaises(ContextBudgetError):
            self.prepare(messages)
        self.assertEqual(self.summary_inputs, [], "无法容纳当前轮时不应浪费摘要调用")

    def test_disabled_compaction_refuses_oversized_input(self):
        messages = sum((turn(i, "alpha beta " * 1200) for i in range(5)), [])
        with self.assertRaises(ContextBudgetError):
            self.prepare(messages, summarize=None)

    def test_offline_900k_boundary_and_compaction_headroom(self):
        below = sum((turn(i, "alpha beta gamma delta " * 54000) for i in range(4)), [])
        above = sum((turn(i, "alpha beta gamma delta " * 58000) for i in range(4)), [])
        settings = ContextSettings()
        short = self.prepare(below, settings=settings)
        self.assertEqual(short.removed_ids, [])
        self.assertLess(short.before_tokens, 900000)
        long = self.prepare(above, settings=settings)
        self.assertGreaterEqual(long.before_tokens, 900000)
        self.assertLessEqual(long.after_tokens, 650000)
        self.assertTrue(long.removed_ids)
        self.assertEqual(long.messages[-2:], above[-2:])

    def test_settings_reject_unsafe_or_inverted_budgets(self):
        for kwargs in ({"trigger_tokens": 1100000}, {"target_tokens": 900000},
                       {"summary_tokens": 0}, {"summary_batch_tokens": 100},
                       {"summary_tokens": 600000, "summary_batch_tokens": 900000}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ContextSettings(**kwargs)

    def test_corrupted_tokenizer_is_not_silently_replaced_by_character_count(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tokenizer.json"
            path.write_text("{}")
            with self.assertRaises(ContextBudgetError):
                TokenCounter(path)


class GraphContextTests(unittest.TestCase):
    def setUp(self):
        from app.agents import graph as module
        from app.observability import audit, llm_metrics, tracing
        self.module = module
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for item in (audit, llm_metrics, tracing):
            self.enterContext(patch.object(item, "AUDIT_DB_PATH", root / "audit.sqlite3"))
        self.enterContext(patch("os.path.ismount", return_value=True))
        self.config = {"configurable": {"thread_id": "context-test"}, "recursion_limit": 12}
        self.summary_requests, self.actor_requests, self.source_requests = [], [], []
        self.summary_finish_reason = "stop"
        fixture = self
        class Actor:
            def invoke(self, messages):
                fixture.actor_requests.append(messages)
                return AIMessage(content="上下文已保留。")
        class Summarizer:
            def bind(self, **kwargs):
                return self
            def invoke(self, messages):
                try: payload=json.loads(messages[-1].content)
                except (ValueError,TypeError): payload=None
                if isinstance(payload,dict) and payload.get("mode")=="source_dependency":
                    fixture.source_requests.append(messages)
                    return AIMessage(content=json.dumps({"decision":"ordinary"}))
                fixture.summary_requests.append(messages)
                metadata = {"finish_reason": fixture.summary_finish_reason} if fixture.summary_finish_reason else {}
                return AIMessage(content="用户要求保留所有记录，继续讨论上下文预算。", response_metadata=metadata)
        self.enterContext(patch.object(module, "llm_with_tools", Actor()))
        self.enterContext(patch.object(module, "llm", Summarizer()))
        self.graph = module.build_graph(checkpointer=InMemorySaver(), enable_summarization=True)

    def test_four_small_turns_keep_full_messages_without_old_three_turn_rule(self):
        for i in range(4):
            self.graph.invoke({"messages": [HumanMessage(content=f"第{i}个问题")]}, self.config)
        snapshot = self.graph.get_state(self.config)
        self.assertEqual(sum(isinstance(m, HumanMessage) for m in snapshot.values["messages"]), 4)
        self.assertEqual(self.summary_requests, [])
        self.assertEqual(len(self.source_requests),4)

    def test_compaction_runs_before_model_and_persists_only_complete_old_turn_removals(self):
        with patch.dict(os.environ, {"AGENT_CONTEXT_TRIGGER_TOKENS": "12000", "AGENT_CONTEXT_TARGET_TOKENS": "7000",
                                     "AGENT_SUMMARY_MAX_TOKENS": "512", "AGENT_SUMMARY_BATCH_TOKENS": "4096"}):
            history = sum((turn(i, "alpha beta gamma delta " * 700) for i in range(6)), [])
            old_config = self.graph.update_state(self.config, {"messages": history})
            result = self.graph.invoke({"messages": [HumanMessage(content="当前问题", id="current")]}, self.config)
        self.assertTrue(self.summary_requests)
        self.assertLessEqual(get_token_counter().messages(self.actor_requests[-1], self.module.TOOL_SCHEMAS), 7000)
        self.assertEqual(result["messages"][-2].id, "current")
        self.assertIn("保留所有记录", result["summary"])
        self.assertLess(len(result["messages"]), len(history))
        self.assertEqual(len(self.graph.get_state(old_config).values["messages"]), len(history), "旧 Checkpoint 必须保留原始历史")

    def test_abnormal_summary_completion_keeps_history_and_never_calls_actor(self):
        with patch.dict(os.environ, {"AGENT_CONTEXT_TRIGGER_TOKENS": "12000", "AGENT_CONTEXT_TARGET_TOKENS": "7000",
                                     "AGENT_SUMMARY_MAX_TOKENS": "512", "AGENT_SUMMARY_BATCH_TOKENS": "4096"}):
            for reason in ("length", "content_filter", "tool_calls", None):
                with self.subTest(finish_reason=reason):
                    self.summary_finish_reason = reason
                    config = {"configurable": {"thread_id": f"summary-failure-{reason}"}}
                    history = sum((turn(i, "alpha beta gamma delta " * 700) for i in range(6)), [])
                    checkpoint = self.graph.update_state(config, {"messages": history})
                    calls_before = len(self.actor_requests)
                    with self.assertRaises(ContextBudgetError):
                        self.graph.invoke({"messages": [HumanMessage(content="继续", id="current")]}, config)
                    state = self.graph.get_state(config).values
                    self.assertEqual(state["messages"][:-1], history)
                    self.assertFalse(state.get("summary"))
                    self.assertEqual(len(self.actor_requests), calls_before)
                    self.assertEqual(self.graph.get_state(checkpoint).values["messages"], history)


class ContextApiTests(CitationApiFixture):
    def setUp(self):
        super().setUp()
        from app.agents import graph as module
        self.module = module
        self.enterContext(patch.dict(os.environ, {
            "AGENT_CONTEXT_TRIGGER_TOKENS": "12000", "AGENT_CONTEXT_TARGET_TOKENS": "7000",
            "AGENT_SUMMARY_MAX_TOKENS": "512", "AGENT_SUMMARY_BATCH_TOKENS": "4096",
        }))
        self.summary_reason, self.summary_calls = "stop", 0
        judge, fixture = module.llm, self
        class SummarizerAndJudge:
            def bind(self, **kwargs):
                return self
            def invoke(self, messages):
                if "较早对话数据片段" not in messages[-1].content:
                    return judge.invoke(messages)
                fixture.summary_calls += 1
                return AIMessage(content="COMPACTION_PRIVATE_MEMORY 用户要求保留原始记录。",
                                 response_metadata={"finish_reason": fixture.summary_reason})
        self.enterContext(patch.object(module, "llm", SummarizerAndJudge()))

    def seed_history(self, thread):
        from app.api import main, session_store
        session_store.create_thread(thread)
        history = sum((turn(i, "alpha beta gamma delta " * 700) for i in range(6)), [])
        for message in history:
            session_store.append_message(thread, "user" if isinstance(message, HumanMessage) else "assistant", message.content)
        config = main.make_config(thread)
        checkpoint = self.client.app.state.graph.update_state(config, {"messages": history}, as_node="agent")
        return history, config, checkpoint

    def test_http_and_sse_compact_keep_full_chat_and_replay_without_summary_leak(self):
        from app.api import request_journal, session_store
        for stream in (False, True):
            with self.subTest(stream=stream):
                thread = f"context-api-{stream}"
                history, config, checkpoint = self.seed_history(thread)
                payload = {"thread_id": thread, "request_id": f"context-success-{stream}", "message": "实验准确率是多少？"}
                response = self.client.post("/api/v1/chat/stream" if stream else "/api/v1/chat", json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                data = next(value for name, value in self.events(response.text) if name == "completed") if stream else response.json()
                self.assertEqual(data["citation_report"]["status"], "verified")
                self.assertNotIn("COMPACTION_PRIVATE_MEMORY", response.text)
                state = self.client.app.state.graph.get_state(config).values
                self.assertIn("COMPACTION_PRIVATE_MEMORY", state["summary"])
                self.assertLess(len(state["messages"]), len(history))
                self.assertEqual(self.client.app.state.graph.get_state(checkpoint).values["messages"], history)
                full = session_store.list_messages(thread)["items"]
                self.assertEqual([message["content"] for message in full[:len(history)]], [message.content for message in history])
                self.assertEqual(len(full), len(history) + 2)
                self.assertEqual(request_journal.get_request(payload["request_id"])["status"], "completed")
                calls = self.summary_calls, self.actor.calls
                replay = self.client.post("/api/v1/chat" if stream else "/api/v1/chat/stream", json=payload)
                repeated = replay.json() if stream else next(value for name, value in self.events(replay.text) if name == "completed")
                self.assertEqual(repeated, data)
                self.assertEqual((self.summary_calls, self.actor.calls), calls)

    def test_http_and_sse_summary_truncation_preserve_history_without_completed_event(self):
        from app.api import request_journal, session_store
        self.summary_reason = "length"
        for stream in (False, True):
            with self.subTest(stream=stream):
                thread = f"context-failure-{stream}"
                history, config, _ = self.seed_history(thread)
                payload = {"thread_id": thread, "request_id": f"context-failure-{stream}", "message": "实验准确率是多少？"}
                actor_calls = self.actor.calls
                response = self.client.post("/api/v1/chat/stream" if stream else "/api/v1/chat", json=payload)
                self.assertEqual(response.status_code, 200 if stream else 500)
                self.assertNotIn("COMPACTION_PRIVATE_MEMORY", response.text)
                if stream:
                    self.assertTrue(any(name == "error" for name, _ in self.events(response.text)))
                    self.assertFalse(any(name == "completed" for name, _ in self.events(response.text)))
                self.assertEqual(self.actor.calls, actor_calls)
                self.assertEqual(self.client.app.state.graph.get_state(config).values["messages"][:-1], history)
                self.assertEqual(len(session_store.list_messages(thread)["items"]), len(history) + 1)
                self.assertEqual(request_journal.get_request(payload["request_id"])["status"], "uncertain")
                self.assertFalse(self.client.app.state.graph_lock.locked())


if __name__ == "__main__":
    unittest.main()
