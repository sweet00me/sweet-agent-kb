"""新 Storage RAG 的 API/SSE 回归：真实 Graph、存储与 HTTP；云端边界隔离。"""

import json
import socket
import threading
import time
import uuid
from contextlib import closing
from unittest.mock import patch

from langchain_core.messages import ToolMessage

from app.api import main, request_journal, session_store, streaming
from app.rag import retrieval
from tests.test_citation_api import CitationApiFixture


class RagApiSseTests(CitationApiFixture):
    def send(self, stream, *, thread=None, request=None, message="实验准确率是多少？"):
        payload = {"thread_id": thread or "rag-" + uuid.uuid4().hex[:16],
                   "request_id": request or "req-" + uuid.uuid4().hex, "message": message}
        response = self.client.post("/api/v1/chat/stream" if stream else "/api/v1/chat", json=payload)
        events = self.events(response.text) if stream and response.status_code == 200 else []
        terminal = [data for name, data in events if name in {"completed", "pending_approval"}]
        data = terminal[-1] if terminal else (response.json() if not events else None)
        return response, data, events, payload

    def test_fresh_rag_contract_and_sse_completed_follows_database_commit(self):
        observed = []
        original = streaming.StreamJob.emit
        def observe(job, name, data):
            if name == "completed":
                record = request_journal.get_request(request_id)
                messages = session_store.list_messages(data["thread_id"])["items"]
                observed.append((record["status"], messages[-1]["role"], messages[-1]["content"]))
            return original(job, name, data)
        request_id = "req-" + uuid.uuid4().hex
        with patch.object(streaming.StreamJob, "emit", new=observe):
            response, data, events, _ = self.send(True, request=request_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed, [("completed", "assistant", data["answer"])])
        names = [name for name, _ in events]
        self.assertEqual(names[0], "started")
        self.assertEqual(names[-1], "completed")
        self.assertEqual(names.count("completed"), 1)
        self.assertIn("no-transform", response.headers["cache-control"])
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertEqual("".join(value["text"] for name, value in events if name == "token"), data["answer"])
        self.assertEqual(data["citation_report"]["status"], "verified")
        citation = data["citation_report"]["citations"][0]
        self.assertEqual((citation["source"], citation["chunk_id"], citation["chunk_index"]),
                         ("notes/experiment.md", self.chunk_id, 0))
        self.assertEqual(citation["quotes"], [self.original])

    def test_empty_and_outdated_results_complete_as_refusals_in_both_transports(self):
        rows = self.collection.rows
        for scenario in ("empty", "outdated"):
            self.collection.rows = [] if scenario == "empty" else rows
            if scenario == "outdated":
                self.source.write_text("已更新但还未同步的实验记录")
            for stream in (False, True):
                with self.subTest(scenario=scenario, stream=stream):
                    response, data, events, payload = self.send(stream)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(data["status"], "completed")
                    self.assertEqual(data["citation_report"]["status"], "rejected")
                    self.assertEqual(data["citation_report"]["reason"], "no_evidence" if scenario == "empty" else "source_unavailable")
                    self.assertEqual(data["citation_report"]["citations"], [])
                    self.assertNotIn("92.5%", data["answer"])
                    self.assertEqual(request_journal.get_request(payload["request_id"])["status"], "completed")
                    self.assertFalse(any(name == "error" for name, _ in events))
        self.assertFalse(self.judge_calls)

    def test_model_failure_is_uncertain_sanitized_and_cannot_be_reexecuted(self):
        for stream in (False, True):
            with self.subTest(stream=stream), patch.object(self.actor, "invoke", side_effect=TimeoutError("PRIVATE_UPSTREAM_DETAIL")) as cloud:
                response, data, events, payload = self.send(stream)
                self.assertEqual(response.status_code, 200 if stream else 500)
                self.assertNotIn("PRIVATE_UPSTREAM_DETAIL", response.text)
                if stream:
                    self.assertEqual([name for name, _ in events], ["started", "error"])
                    self.assertIsNone(data)
                else:
                    self.assertEqual(data["detail"]["code"], "AGENT_EXECUTION_FAILED")
                self.assertEqual(request_journal.get_request(payload["request_id"])["status"], "uncertain")
                self.assertFalse(self.client.app.state.graph_lock.locked())
                retry = self.client.post("/api/v1/chat/stream" if stream else "/api/v1/chat", json=payload)
                self.assertEqual(retry.status_code, 409)
                self.assertEqual(cloud.call_count, 1)
                self.assertEqual(len(session_store.list_messages(payload["thread_id"])["items"]), 1)

    def test_citation_judge_failure_is_a_safe_completed_refusal(self):
        from app.agents import graph as graph_module
        for stream in (False, True):
            with self.subTest(stream=stream), patch.object(graph_module.llm, "invoke", side_effect=TimeoutError("PRIVATE_JUDGE_DETAIL")):
                response, data, events, payload = self.send(stream)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(data["citation_report"]["status"], "rejected")
                self.assertEqual(data["citation_report"]["reason"], "verification_unavailable")
                self.assertNotIn("PRIVATE_JUDGE_DETAIL", response.text)
                self.assertNotIn("92.5%", data["answer"])
                self.assertEqual(request_journal.get_request(payload["request_id"])["status"], "completed")

    def test_embedding_failure_does_not_leak_details_or_repeat_query(self):
        for stream in (False, True):
            with self.subTest(stream=stream), patch.object(retrieval, "_embed_question", side_effect=TimeoutError("PRIVATE_EMBEDDING_DETAIL")) as embed:
                response, data, events, payload = self.send(stream)
                self.assertEqual(response.status_code, 200 if stream else 500)
                self.assertNotIn("PRIVATE_EMBEDDING_DETAIL", response.text)
                self.assertEqual(request_journal.get_request(payload["request_id"])["status"], "uncertain")
                self.assertFalse(any(name == "completed" for name, _ in events))
                retry = self.client.post("/api/v1/chat/stream" if stream else "/api/v1/chat", json=payload)
                self.assertEqual(retry.status_code, 409)
                self.assertEqual(embed.call_count, 1)

    def test_excluded_source_never_reaches_http_or_sse_answer(self):
        row = self.collection.rows[0]
        self.collection.rows.insert(0, dict(row, id="0" * 32, text="PRIVATE_EXCLUDED_CONTENT", distance=0.01,
            meta=dict(row["meta"], source="KnowledgeBase/documents/Diary/24.7.16.md")))
        for stream in (False, True):
            response, data, _, _ = self.send(stream)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(data["citation_report"]["status"], "verified")
            self.assertNotIn("PRIVATE_EXCLUDED_CONTENT", response.text)
            self.assertNotIn("24.7.16.md", response.text)

    def test_cross_transport_replay_preserves_report_without_retrieval_or_extra_history(self):
        for initial_stream in (False, True):
            with self.subTest(initial_stream=initial_stream):
                response, data, _, payload = self.send(initial_stream)
                self.assertEqual(response.status_code, 200)
                calls = self.actor.calls
                judge_calls = len(self.judge_calls)
                replay, repeated, _, _ = self.send(not initial_stream, thread=payload["thread_id"], request=payload["request_id"])
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(repeated, data)
                self.assertEqual(self.actor.calls, calls)
                self.assertEqual(len(self.judge_calls), judge_calls)
                self.assertEqual(len(session_store.list_messages(payload["thread_id"])["items"]), 2)

    def test_request_id_conflict_never_changes_history_or_reruns_rag(self):
        _, _, _, payload = self.send(False)
        calls = self.actor.calls
        for stream in (False, True):
            with self.subTest(stream=stream):
                response, data, _, _ = self.send(stream, thread=payload["thread_id"], request=payload["request_id"], message="篡改了的问题")
                self.assertEqual(response.status_code, 409)
                self.assertEqual(data["detail"]["code"], "REQUEST_ID_CONFLICT")
        self.assertEqual(self.actor.calls, calls)
        self.assertEqual(len(session_store.list_messages(payload["thread_id"])["items"]), 2)

    def test_commit_failure_rolls_back_history_and_never_sends_completed(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                with closing(session_store.connect_db()) as connection:
                    connection.executescript("""CREATE TRIGGER fail_rag_completion BEFORE UPDATE OF status ON api_requests
                        WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT, 'PRIVATE_SQL_DETAIL'); END;""")
                try:
                    response, data, events, payload = self.send(stream)
                finally:
                    with closing(session_store.connect_db()) as connection:
                        connection.execute("DROP TRIGGER fail_rag_completion")
                        connection.commit()
                self.assertEqual(response.status_code, 200 if stream else 500)
                self.assertNotIn("PRIVATE_SQL_DETAIL", response.text)
                self.assertFalse(any(name == "completed" for name, _ in events))
                self.assertEqual(request_journal.get_request(payload["request_id"])["status"], "uncertain")
                self.assertEqual([m["role"] for m in session_store.list_messages(payload["thread_id"])["items"]], ["user"])
                self.assertFalse(self.client.app.state.graph_lock.locked())

    def test_pending_rag_approval_replay_and_http_sse_resume(self):
        self.actor.review = True
        for initial_stream, review_stream in ((False, True), (True, False)):
            with self.subTest(initial_stream=initial_stream):
                response, pending, _, payload = self.send(initial_stream)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(pending["status"], "pending_approval")
                self.assertIsNone(pending["answer"])
                self.assertEqual(len(session_store.list_messages(payload["thread_id"])["items"]), 1)
                calls = self.actor.calls
                replay, repeated, _, _ = self.send(not initial_stream, thread=payload["thread_id"], request=payload["request_id"])
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(repeated, pending)
                self.assertEqual(self.actor.calls, calls)
                call = pending["pending_approvals"][0]
                body = {"decisions": {call["tool_call_id"]: "reject"},
                        "expected_fingerprints": {call["tool_call_id"]: call["fingerprint"]},
                        "request_id": "req-" + uuid.uuid4().hex}
                path = f'/api/v1/threads/{payload["thread_id"]}/review' + ("/stream" if review_stream else "")
                reviewed = self.client.post(path, json=body)
                self.assertEqual(reviewed.status_code, 200, reviewed.text)
                result = next(data for name, data in self.events(reviewed.text) if name == "completed") if review_stream else reviewed.json()
                self.assertEqual(result["citation_report"]["status"], "verified")
                self.assertEqual(result["citation_report"]["citations"][0]["chunk_id"], self.chunk_id)
                self.assertEqual(len(session_store.list_messages(payload["thread_id"])["items"]), 2)

    def test_busy_sse_request_is_rejected_before_registration(self):
        lock = self.client.app.state.graph_lock
        lock.acquire()
        try:
            response, data, _, payload = self.send(True)
        finally:
            lock.release()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(data["detail"]["code"], "AGENT_BUSY")
        self.assertIsNone(request_journal.get_request(payload["request_id"]))
        self.assertIsNone(session_store.get_thread(payload["thread_id"]))

    def test_network_disconnect_keeps_background_rag_and_replay_does_not_duplicate(self):
        import httpx
        import uvicorn
        gate, entered = threading.Event(), threading.Event()
        invoke = self.actor.invoke
        def delayed(messages):
            if any(isinstance(message, ToolMessage) for message in messages):
                entered.set()
                if not gate.wait(5):
                    raise TimeoutError("测试未释放模型调用")
            return invoke(messages)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(main.app, lifespan="off", access_log=False, log_level="critical"))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(server.started)
            payload = {"thread_id": "rag-disconnect", "request_id": "req-disconnect", "message": "实验准确率是多少？"}
            with patch.object(self.actor, "invoke", side_effect=delayed), httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
                with client.stream("POST", "/api/v1/chat/stream", json=payload) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(next(response.iter_lines()), "event: started")
                    self.assertTrue(entered.wait(3))
                # 已关闭真实 TCP 响应，后台模型此时仍阻塞。
                self.assertEqual(request_journal.get_request(payload["request_id"])["status"], "running")
                gate.set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    record = request_journal.get_request(payload["request_id"])
                    if record["status"] == "completed" and not main.app.state.graph_lock.locked():
                        break
                    time.sleep(0.01)
                self.assertEqual(record["status"], "completed")
                replay = client.post("/api/v1/chat/stream", json=payload)
                events = self.events(replay.text)
                self.assertEqual([name for name, _ in events], ["completed"])
                self.assertEqual(events[0][1]["citation_report"]["status"], "verified")
                self.assertEqual(self.actor.calls, 2)
                self.assertEqual(len(self.collection.queries), 1)
                self.assertEqual([m["role"] for m in session_store.list_messages(payload["thread_id"])["items"]], ["user", "assistant"])
        finally:
            gate.set()
            server.should_exit = True
            worker.join(6)
            listener.close()
        self.assertFalse(worker.is_alive(), "临时 HTTP 服务未退出")


if __name__ == "__main__":
    import unittest
    unittest.main()
