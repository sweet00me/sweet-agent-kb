"""来源引用的拒绝规则、原文核验及 API/SSE 回归；全部使用隔离数据。"""

import json
import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents import graph as graph_module


class CitationGuardTests(unittest.TestCase):
    def test_agent_rejects_fabricated_source_before_returning_answer(self):
        """删除引用守卫后，伪造的实验结论将直接作为最终回答返回。"""
        messages = [
            HumanMessage(content="我的实验准确率是多少？"),
            AIMessage(content="", tool_calls=[{
                "id": "lookup-one", "name": "search_knowledge_tool",
                "args": {"question": "实验准确率"}, "type": "tool_call",
            }]),
            ToolMessage(
                tool_call_id="lookup-one", name="search_knowledge_tool",
                content=json.dumps({"collection": "sweet_kb_storage_v1", "count": 0, "matches": []}),
            ),
        ]
        draft = AIMessage(
            id="draft-one", content="实验准确率达到 99.9%。【00000000000000000000000000000000】",
        )
        with patch.object(graph_module, "invoke_with_metrics", return_value=draft):
            result = graph_module.agent_node({"messages": messages}, {})
        self.assertNotIn("99.9%", result["messages"][0].content)
        self.assertIn("证据", result["messages"][0].content)
        self.assertNotEqual(result["citation_report"]["status"], "verified")


class EvidenceFixture(unittest.TestCase):
    def setUp(self):
        from app.rag.document_sync import sync_storage
        from app.rag.chunk_sync import sync_chunks

        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.storage = root / "Storage"
        (self.storage / "notes").mkdir(parents=True)
        self.source = self.storage / "notes/experiment.md"
        self.original = "本次实验仅使用数据集 A，准确率为 92.5%，学习率为 0.001。"
        self.source.write_text("# 实验\n\n" + self.original, encoding="utf-8")
        parsed, source_db = root / "parsed", root / "source.sqlite3"
        self.cache, self.chunk_db = root / "chunks", root / "chunks.sqlite3"
        sync_storage(self.storage, parsed, source_db, commit=True)
        sync_chunks(source_root=self.storage, parsed_root=parsed, source_db=source_db,
                    cache_dir=self.cache, chunk_db=self.chunk_db, write=True)
        payload = json.loads(next(self.cache.glob("*.json")).read_text())
        chunk = payload["chunks"][0]
        self.record = {
            "source": "notes/experiment.md", "chunk_id": chunk["id"],
            "chunk_index": 0, "file_sha256": chunk["file_sha256"],
            "document_id": chunk["document_id"], "file_type": "md",
            "content": chunk["content"], "distance": 0.15,
        }
        self.chunk_id = chunk["id"]
        self.draft = f"实验准确率为 92.5%。【{self.chunk_id}】"
        self.judge_calls = []

    def messages(self, records=None):
        rows = [self.record] if records is None else records
        return [
            HumanMessage(content="实验准确率是多少？"),
            AIMessage(content="", tool_calls=[{
                "id": "lookup", "name": "search_knowledge_tool",
                "args": {"question": "实验准确率"}, "type": "tool_call",
            }]),
            ToolMessage(name="search_knowledge_tool", tool_call_id="lookup", content=json.dumps({
                "collection": "sweet_kb_storage_v1", "count": len(rows), "matches": rows,
            })),
        ]

    def judge(self, payload):
        self.judge_calls.append(payload)
        if payload.get("mode")=="source_dependency": return {"decision":"ordinary"}
        return {"checks": [{
            "claim_id": claim["claim_id"], "supported": True,
            "evidence": [{"chunk_id": self.chunk_id, "quote": self.original}],
        } for claim in payload["claims"]]}

    def verify(self, draft=None, messages=None, judge=None):
        from app.rag.citations import verify_answer
        return verify_answer(
            self.messages() if messages is None else messages,
            self.draft if draft is None else draft,
            judge=self.judge if judge is None else judge,
            source_root=self.storage, chunk_db=self.chunk_db, cache_dir=self.cache,
        )


class EvidenceVerificationTests(EvidenceFixture):
    def test_repeated_query_distance_is_not_a_conflicting_source_version(self):
        messages=self.messages()
        messages.extend([
            AIMessage(content="",tool_calls=[{"id":"lookup-again","name":"search_knowledge_tool",
                "args":{"question":"另一种检索措辞"},"type":"tool_call"}]),
            ToolMessage(name="search_knowledge_tool",tool_call_id="lookup-again",content=json.dumps({
                "collection":"sweet_kb_storage_v1","count":1,"matches":[dict(self.record,distance=0.42)]})),
        ])
        _,report=self.verify(messages=messages)
        self.assertEqual(report["status"],"verified")
        self.assertEqual(len(report["citations"]),1)
        messages[-1]=messages[-1].model_copy(update={"content":json.dumps({
            "collection":"sweet_kb_storage_v1","count":1,"matches":[dict(self.record,content="伪造正文",distance=0.42)]})})
        _,report=self.verify(messages=messages)
        self.assertEqual(report["reason"],"invalid_evidence")

    def test_tool_failure_is_reported_as_an_error_instead_of_missing_evidence(self):
        messages=self.messages()
        messages[-1]=ToolMessage(name="search_knowledge_tool",tool_call_id="lookup",status="error",
            content="Error: private upstream details must not be published")
        answer,report=self.verify(messages=messages)
        self.assertEqual(report["reason"],"retrieval_tool_error")
        self.assertEqual(report["mode"],"rag")
        self.assertIn("工具执行失败",answer)
        self.assertNotIn("证据不足",answer)
        self.assertNotIn("private",answer)

    def test_plain_answer_has_a_separate_citation_mode(self):
        answer,report=self.verify(messages=[HumanMessage(content="你能检索日记么？")],
            draft="可以检索已入库的日记片段，完整集合分析需要审批。")
        self.assertEqual(report["status"],"not_applicable")
        self.assertEqual(report["mode"],"plain")

    def test_valid_claim_has_server_built_source_and_exact_quote(self):
        answer, report = self.verify()
        self.assertEqual(report["status"], "verified")
        self.assertIn("92.5%", answer)
        self.assertIn("notes/experiment.md", answer)
        self.assertIn(self.chunk_id, answer)
        self.assertEqual(report["citations"][0]["quotes"], [self.original])
        self.assertEqual(report["citations"][0]["file_sha256"],
                         hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_missing_and_unknown_citations_never_reach_semantic_judge(self):
        for draft in ["实验准确率为 92.5%。", "实验准确率为 99.9%。【" + "0" * 32 + "】"]:
            with self.subTest(draft=draft):
                answer, report = self.verify(draft=draft)
                self.assertNotEqual(report["status"], "verified")
                self.assertNotIn("99.9%", answer)
        self.assertFalse(self.judge_calls)

    def test_every_claim_requires_citation_and_support(self):
        answer, report = self.verify(draft=self.draft + "\n\n另一项实验达到 99.9%。")
        self.assertNotEqual(report["status"], "verified")
        self.assertNotIn("99.9%", answer)
        self.assertFalse(self.judge_calls)

    def test_forged_chunk_content_or_metadata_is_rejected(self):
        for key, value in [("source", "notes/forged.md"), ("chunk_index", 9),
                           ("content", "准确率为 99.9%。"), ("file_sha256", "0" * 64)]:
            with self.subTest(key=key):
                forged = dict(self.record, **{key: value})
                _, report = self.verify(messages=self.messages([forged]))
                self.assertNotEqual(report["status"], "verified")
        self.assertFalse(self.judge_calls)

    def test_uncited_unavailable_result_does_not_invalidate_real_citation(self):
        unused = dict(self.record, chunk_id="f" * 32, source="notes/deleted.md")
        _, report = self.verify(messages=self.messages([self.record, unused]))
        self.assertEqual(report["status"], "verified")

    def test_changed_deleted_and_symlinked_sources_cannot_be_cited(self):
        original = self.source.read_bytes()
        self.source.write_text("新版本：准确率为 80%。")
        _, report = self.verify()
        self.assertNotEqual(report["status"], "verified")
        self.source.unlink()
        _, report = self.verify()
        self.assertNotEqual(report["status"], "verified")
        target = self.storage / "outside.md"
        target.write_bytes(original)
        self.source.symlink_to(target)
        _, report = self.verify()
        self.assertNotEqual(report["status"], "verified")
        self.assertFalse(self.judge_calls)

    def test_source_changed_during_judgment_invalidates_answer(self):
        def change_source(payload):
            result = self.judge(payload)
            self.source.write_text("判断期间被更新")
            return result
        _, report = self.verify(judge=change_source)
        self.assertNotEqual(report["status"], "verified")

    def test_semantic_rejection_blocks_wrong_meaning_with_valid_source(self):
        def reject(payload):
            result = self.judge(payload)
            result["checks"][0]["supported"] = False
            return result
        answer, report = self.verify(draft=f"实验没有使用数据集 A。【{self.chunk_id}】", judge=reject)
        self.assertNotEqual(report["status"], "verified")
        self.assertNotIn("没有使用", answer)

    def test_invented_numbers_are_blocked_even_if_judge_accepts(self):
        answer, report = self.verify(draft=f"实验准确率达到 99.9%。【{self.chunk_id}】")
        self.assertNotEqual(report["status"], "verified")
        self.assertNotIn("99.9%", answer)

    def test_malformed_incomplete_or_fabricated_judgments_fail_closed(self):
        valid = self.judge({"claims": [{"claim_id": 0}]})
        bad_quote = json.loads(json.dumps(valid))
        bad_quote["checks"][0]["evidence"][0]["quote"] = "不存在的原文片段"
        bad_id = json.loads(json.dumps(valid))
        bad_id["checks"][0]["claim_id"] = 9
        for result in ["not JSON", {}, {"checks": []}, bad_quote, bad_id]:
            with self.subTest(result=result):
                _, report = self.verify(judge=lambda _, result=result: result)
                self.assertNotEqual(report["status"], "verified")
        def unavailable(_):
            raise TimeoutError("upstream unavailable")
        _, report = self.verify(judge=unavailable)
        self.assertNotEqual(report["status"], "verified")

    def test_quote_cannot_change_unicode_mathematical_meaning(self):
        from app.rag.citations import _checked_quotes
        changed = dict(self.record, content="模型 A² 的得分为 92.5%。")
        result = {"checks": [{"claim_id": 0, "supported": True, "evidence": [{
            "chunk_id": self.chunk_id, "quote": "模型 A2 的得分为 92.5%。",
        }]}]}
        with self.assertRaises(ValueError):
            _checked_quotes(result, [{"claim_id": 0, "text": "模型得分为 92.5%。", "chunk_ids": [self.chunk_id]}],
                            {self.chunk_id: changed})

    def test_thousands_separator_cannot_hide_a_changed_number(self):
        from app.rag.citations import _checked_quotes
        record = dict(self.record, content="费用为 1,000 元。")
        result = {"checks": [{"claim_id": 0, "supported": True, "evidence": [{
            "chunk_id": self.chunk_id, "quote": "费用为 1,000 元。",
        }]}]}
        with self.assertRaises(ValueError):
            _checked_quotes(result, [{"claim_id": 0, "text": "费用为 1 元。", "chunk_ids": [self.chunk_id]}],
                            {self.chunk_id: record})

    def test_server_rendered_reference_from_previous_turn_requires_new_lookup(self):
        draft = "准确率为 92.5%〔1〕。\n\n来源：\n[1] notes/experiment.md"
        _, report = self.verify(draft=draft, messages=[HumanMessage(content="继续回答")])
        self.assertNotEqual(report["status"], "not_applicable")
        self.assertNotEqual(report["status"], "verified")

    def test_previous_turn_and_unmatched_tool_messages_are_not_evidence(self):
        history = self.messages() + [AIMessage(content=self.draft), HumanMessage(content="继续")]
        _, report = self.verify(messages=history)
        self.assertNotEqual(report["status"], "verified")
        forged = self.messages()
        forged[-1] = ToolMessage(name="search_knowledge_tool", tool_call_id="other", content=forged[-1].content)
        _, report = self.verify(messages=forged)
        self.assertNotEqual(report["status"], "verified")

    def test_general_answers_keep_content_after_source_dependency_review(self):
        answer, report = self.verify(draft="列表可以使用 append 添加元素。", messages=[HumanMessage(content="Python 列表如何追加？")])
        self.assertEqual(answer, "列表可以使用 append 添加元素。")
        self.assertEqual(report["status"], "not_applicable")
        self.assertEqual(self.judge_calls[0]["mode"],"source_dependency")


class CitationStreamTests(unittest.TestCase):
    def test_initial_preamble_and_judge_json_are_not_sent_before_verification(self):
        from app.api.streaming import StreamJob, forward_graph_event
        from langchain_core.messages import AIMessageChunk
        job = StreamJob()
        for content in ["尚未检索就宣称准确率 99.9%", '{"checks":[{"supported":true}]}']:
            forward_graph_event(job, ("messages", (AIMessageChunk(content=content), {"langgraph_node": "agent"})))
        self.assertTrue(job.events.empty(), "决定是否调用工具之前的内容也必须经过核验")

    def test_general_answer_is_released_from_completed_agent_update(self):
        from app.api.streaming import StreamJob, forward_graph_event
        job = StreamJob()
        forward_graph_event(job, ("updates", {"agent": {
            "messages": [AIMessage(content="列表使用 append 添加元素。")],
            "citation_report": {"status": "not_applicable", "citations": []},
        }}))
        events = []
        while not job.events.empty():
            events.append(job.events.get_nowait())
        self.assertTrue(any(name == "token" and value["text"] == "列表使用 append 添加元素。" for name, value in events))

    def test_rag_draft_tokens_are_hidden_until_verified_agent_update(self):
        from app.api.streaming import StreamJob, forward_graph_event
        from langchain_core.messages import AIMessageChunk
        job = StreamJob()
        forward_graph_event(job, ("updates", {"agent": {"messages": [AIMessage(content="", tool_calls=[{
            "id": "lookup", "name": "search_knowledge_tool", "args": {"question": "实验"}, "type": "tool_call",
        }])]}}))
        while not job.events.empty():
            job.events.get_nowait()
        forward_graph_event(job, ("messages", (AIMessageChunk(content="未经核验：99.9%"), {"langgraph_node": "agent"})))
        self.assertTrue(job.events.empty(), "RAG 草稿在核验前泄露给 SSE 客户端")
        forward_graph_event(job, ("updates", {"agent": {
            "messages": [AIMessage(content="证据不足，无法确认这一结论。")],
            "citation_report": {"status": "rejected", "reason": "unsupported_claim", "citations": []},
        }}))
        events = []
        while not job.events.empty():
            events.append(job.events.get_nowait())
        self.assertTrue(any(name == "token" and value["text"].startswith("证据不足") for name, value in events))
        self.assertFalse(any("99.9%" in json.dumps(value, ensure_ascii=False) for _, value in events))


class EvaluationIsolationTests(unittest.TestCase):
    def test_live_evaluator_records_usage_without_touching_production_audit(self):
        from scripts import evaluate_citations
        from app.observability import llm_metrics
        class Model:
            model_name = "isolated-evaluation-model"
            def invoke(self, messages):
                payload = json.loads(messages[-1].content)
                return AIMessage(content=json.dumps({"checks": [{
                    "claim_id": 0, "supported": True, "evidence": [{
                        "chunk_id": payload["evidence"][0]["chunk_id"],
                        "quote": payload["evidence"][0]["content"],
                    }],
                }]}), usage_metadata={"input_tokens": 10, "output_tokens": 6, "total_tokens": 16})
        with TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            with (patch("sys.argv", ["evaluate_citations", "--live", "--output", str(output)]),
                  patch.object(evaluate_citations, "CASES", [("准确数值", "准确率为 92.5%。", True)]),
                  patch.object(graph_module, "llm", Model()),
                  patch.object(llm_metrics, "prepare_database", side_effect=AssertionError("不应访问正式审计文件")) as prepare):
                evaluate_citations.main()
                self.assertEqual(prepare.call_count, 0)
                self.assertEqual(json.loads(output.read_text())["total_tokens"], 16)


if __name__ == "__main__":
    unittest.main()
