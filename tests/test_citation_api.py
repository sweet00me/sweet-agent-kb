"""真实 Graph/RAG/SQLite/HTTP/SSE 联动；仅替换云模型、向量客户端和存储路径。"""

import json
import uuid
from functools import partial
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from app.agents import graph as graph_module
from app.api import main, session_store, request_journal
from app.observability import audit, llm_metrics, tracing
from app.memory import checkpoint
from app.rag import citations, retrieval
from tests.test_citation_verification import EvidenceFixture
from tests.test_storage_retrieval import FakeCollection


class ScriptedAgent:
    def __init__(self, draft, *, review=False):
        self.draft, self.review, self.calls = draft, review, 0

    def invoke(self, messages):
        self.calls += 1
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if not results:
            return AIMessage(content="", tool_calls=[{
                "id": "lookup", "name": "search_knowledge_tool",
                "args": {"question": "实验准确率", "top_k": 1}, "type": "tool_call",
            }])
        if self.review and not any(m.name == "simulate_save_note" for m in results):
            return AIMessage(content="", tool_calls=[{
                "id": "save-one", "name": "simulate_save_note",
                "args": {"title": "测试", "content": "模拟保存，不写入知识库"}, "type": "tool_call",
            }])
        return AIMessage(content=self.draft)


class CitationApiFixture(EvidenceFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict("os.environ", {"RAG_AUTO_SYNC_ENABLED": "0"}))
        self.actor = ScriptedAgent(self.draft)
        row = {"id": self.chunk_id, "text": self.record["content"], "distance": 0.15,
               "meta": {key: self.record[key] for key in (
                   "source", "chunk_index", "file_sha256", "document_id", "file_type",
               )} | {"is_active": True}}
        self.collection = FakeCollection([row])
        root = self.storage.parent
        self.checkpoint_path = root / "checkpoint.sqlite3"
        for module in (audit, llm_metrics, tracing):
            self.enterContext(patch.object(module, "AUDIT_DB_PATH", root / "audit.sqlite3"))
        self.enterContext(patch.object(session_store, "SESSION_DB_PATH", root / "sessions.sqlite3"))
        self.enterContext(patch.object(session_store, "STORAGE_ROOT", self.storage))
        self.enterContext(patch.object(session_store.os.path, "ismount", return_value=True))
        self.enterContext(patch.object(checkpoint, "DB_PATH", self.checkpoint_path))
        self.enterContext(patch.object(checkpoint, "STORAGE_ROOT", self.storage))
        self.enterContext(patch.object(retrieval, "_open_real_collection", return_value=self.collection))
        self.enterContext(patch.object(retrieval, "_embed_question", return_value=[0.1] * 1024))
        self.enterContext(patch.object(graph_module, "verify_answer", partial(
            citations.verify_answer, source_root=self.storage, chunk_db=self.chunk_db, cache_dir=self.cache,
        )))
        self.enterContext(patch.object(graph_module, "llm_with_tools", self.actor))
        fixture = self
        class Judge:
            def invoke(self, messages):
                return AIMessage(content=json.dumps(fixture.judge(json.loads(messages[-1].content))))
        self.enterContext(patch.object(graph_module, "llm", Judge()))
        self.client = self.enterContext(TestClient(main.app))

    def request(self, stream=False):
        thread_id = "citation-" + uuid.uuid4().hex[:16]
        request_id = "req-" + uuid.uuid4().hex
        payload = {"message": "实验准确率是多少？", "thread_id": thread_id, "request_id": request_id}
        path = "/api/v1/chat/stream" if stream else "/api/v1/chat"
        response = self.client.post(path, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        if stream:
            events = self.events(response.text)
            response_data = next(data for name, data in events if name == "completed")
        else:
            events, response_data = [], response.json()
        return thread_id, request_id, path, payload, response_data, events

    @staticmethod
    def events(text):
        events = []
        for frame in text.split("\n\n"):
            event, payload = None, None
            for line in frame.splitlines():
                if line.startswith("event: "):
                    event = line[7:]
                if line.startswith("data: "):
                    payload = json.loads(line[6:])
            if event is not None:
                events.append((event, payload))
        return events


class CitationApiTests(CitationApiFixture):
    def test_http_and_sse_share_guard_and_persist_only_verified_answer(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                thread, request, path, payload, data, events = self.request(stream)
                self.assertEqual(data["citation_report"]["status"], "verified")
                self.assertIn("notes/experiment.md", data["answer"])
                self.assertEqual(data["citation_report"]["citations"][0]["chunk_id"], self.chunk_id)
                record = request_journal.get_request(request)
                self.assertEqual(record["response"]["citation_report"], data["citation_report"])
                self.assertIn("citation_check", [m["call_kind"] for m in llm_metrics.list_llm_metrics(thread)])
                self.assertEqual(session_store.list_messages(thread)["items"][-1]["content"], data["answer"])
                replay = self.client.post(path, json=payload)
                if stream:
                    repeated = next(value for name, value in self.events(replay.text) if name == "completed")
                else:
                    repeated = replay.json()
                self.assertEqual(repeated["answer"], data["answer"])

    def test_http_and_sse_reject_fabricated_numeric_claim(self):
        self.actor.draft = f"实验准确率达到 99.9%。【{self.chunk_id}】"
        for stream in (False, True):
            with self.subTest(stream=stream):
                thread, _, _, _, data, events = self.request(stream)
                self.assertEqual(data["citation_report"]["status"], "rejected")
                self.assertNotIn("99.9%", data["answer"])
                self.assertTrue(all("99.9%" not in json.dumps(value) for _, value in events))
                self.assertNotIn("99.9%", session_store.list_messages(thread)["items"][-1]["content"])

    def test_review_resume_keeps_same_turn_evidence_and_verification(self):
        self.actor.review = True
        thread = "citation-review"
        first = self.client.post("/api/v1/chat", json={"thread_id": thread, "message": "查询实验并模拟保存"})
        self.assertEqual(first.status_code, 200, first.text)
        pending = first.json()
        self.assertEqual(pending["status"], "pending_approval")
        call = pending["pending_approvals"][0]
        response = self.client.post(f"/api/v1/threads/{thread}/review/stream", json={
            "decisions": {call["tool_call_id"]: "reject"},
            "expected_fingerprints": {call["tool_call_id"]: call["fingerprint"]},
            "request_id": "req-review-" + uuid.uuid4().hex,
        })
        self.assertEqual(response.status_code, 200, response.text)
        completed = next(data for name, data in self.events(response.text) if name == "completed")
        self.assertEqual(completed["citation_report"]["status"], "verified")
        self.assertIn(self.chunk_id, completed["answer"])

    def test_checkpoint_report_survives_reopen_without_request_id(self):
        response = self.client.post("/api/v1/chat", json={
            "message": "实验准确率是多少？", "thread_id": "persistent-citation",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["citation_report"]["status"], "verified")
        self.assertTrue(self.checkpoint_path.is_file())
        with checkpoint.open_checkpointer() as saver:
            reopened = graph_module.build_graph(checkpointer=saver)
            state = reopened.get_state({"configurable": {"thread_id": "persistent-citation"}})
            self.assertEqual(state.values["citation_report"], response.json()["citation_report"])
            self.assertEqual(state.values["messages"][-1].content, response.json()["answer"])


if __name__ == "__main__":
    import unittest
    unittest.main()
