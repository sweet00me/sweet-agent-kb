"""会话重命名、软删除与旧版数据库迁移的离线回归测试。"""

import importlib
import sqlite3
import threading
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import request_journal, session_store


class SessionFixture(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.db_path = root / "sessions.sqlite3"
        self.enterContext(patch.object(session_store, "SESSION_DB_PATH", self.db_path))
        self.enterContext(patch.object(session_store, "STORAGE_ROOT", root))
        self.enterContext(patch.object(session_store.os.path, "ismount", return_value=True))
        session_store.initialize_database()
        session_store.create_thread("research-one", "原始标题")
        session_store.append_message("research-one", "user", "原始问题")
        session_store.append_message("research-one", "assistant", "原始回答")
        session_store.create_thread("research-two", "其他会话")

    def events(self):
        with closing(session_store.connect_db()) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM chat_thread_events ORDER BY id"
            )]


class ThreadStoreTests(SessionFixture):
    def test_rename_preserves_history_and_records_previous_title(self):
        original = session_store.get_thread("research-one")
        renamed = session_store.rename_thread("research-one", "  新标题  ")
        self.assertEqual(renamed["title"], "新标题")
        self.assertEqual(renamed["created_at"], original["created_at"])
        self.assertEqual(len(session_store.list_messages("research-one")["items"]), 2)
        self.assertEqual(session_store.get_thread("research-two")["title"], "其他会话")
        event = self.events()[0]
        self.assertEqual(event["event_type"], "renamed")
        self.assertEqual(event["previous_title"], "原始标题")
        self.assertEqual(event["title"], "新标题")

    def test_delete_hides_thread_but_preserves_messages_and_local_event(self):
        self.assertTrue(session_store.delete_thread("research-one"))
        self.assertIsNone(session_store.get_thread("research-one"))
        self.assertEqual(
            [row["thread_id"] for row in session_store.list_threads()],
            ["research-two"],
        )
        with closing(session_store.connect_db()) as conn:
            row = conn.execute(
                "SELECT title, deleted_at FROM chat_threads WHERE thread_id = ?",
                ("research-one",),
            ).fetchone()
            self.assertEqual(row["title"], "原始标题")
            self.assertIsNotNone(row["deleted_at"])
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE thread_id = ?",
                ("research-one",),
            ).fetchone()[0], 2)
        self.assertEqual(self.events()[0]["event_type"], "deleted")
        session_store.initialize_database()
        self.assertIsNone(session_store.get_thread("research-one"))

    def test_deleted_thread_cannot_be_renamed_or_recreated(self):
        session_store.delete_thread("research-one")
        self.assertIsNone(session_store.rename_thread("research-one", "不应复活"))
        self.assertFalse(session_store.delete_thread("research-one"))
        with self.assertRaises(ValueError):
            session_store.ensure_thread("research-one")
        self.assertIsNone(session_store.get_thread("research-one"))
        self.assertEqual(len(self.events()), 1)

    def test_audit_failure_rolls_back_deletion(self):
        with closing(session_store.connect_db()) as conn:
            conn.executescript("""
                CREATE TRIGGER fail_thread_event
                BEFORE INSERT ON chat_thread_events
                BEGIN
                    SELECT RAISE(ABORT, 'simulated audit failure');
                END;
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            session_store.delete_thread("research-one")
        self.assertIsNotNone(session_store.get_thread("research-one"))

    def test_legacy_database_migrates_without_losing_existing_history(self):
        legacy_path = self.db_path.with_name("legacy.sqlite3")
        with sqlite3.connect(legacy_path) as conn:
            conn.executescript("""
                CREATE TABLE chat_threads (
                    thread_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO chat_threads VALUES ('legacy', '旧会话', 'old', 'old');
                INSERT INTO chat_messages (thread_id, role, content, created_at)
                VALUES ('legacy', 'user', '旧消息', 'old');
            """)
        with patch.object(session_store, "SESSION_DB_PATH", legacy_path):
            session_store.initialize_database()
            session_store.initialize_database()
            with closing(session_store.connect_db()) as conn:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(chat_threads)")}
            self.assertIn("deleted_at", columns)
            self.assertEqual(session_store.get_thread("legacy")["title"], "旧会话")
            self.assertEqual(session_store.list_messages("legacy")["items"][0]["content"], "旧消息")
            self.assertTrue(session_store.delete_thread("legacy"))


class NoAgentExecution:
    def get_state(self, *args, **kwargs):
        raise AssertionError("会话管理不应读取或运行 Agent")


class ThreadApiTests(SessionFixture):
    def setUp(self):
        super().setUp()
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-local-only"}):
            self.api = importlib.import_module("app.api.main")
        self.enterContext(patch.object(self.api.app.state, "graph", NoAgentExecution(), create=True))
        self.lock = threading.Lock()
        self.enterContext(patch.object(self.api.app.state, "graph_lock", self.lock, create=True))
        # 不进入 TestClient 生命周期，避免打开正式数据库或 Checkpointer。
        self.client = TestClient(self.api.app)
        self.addCleanup(self.client.close)

    def test_patch_renames_thread_and_returns_updated_record(self):
        response = self.client.patch(
            "/api/v1/threads/research-one", json={"title": "  接口改名  "}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["title"], "接口改名")
        self.assertEqual(self.client.get("/api/v1/threads/research-one").json()["title"], "接口改名")
        self.assertEqual(len(self.client.get("/api/v1/threads/research-one/messages").json()["items"]), 2)

    def test_patch_rejects_blank_oversized_or_non_string_titles(self):
        for title in ("", " \n\t ", "名" * 81, None, 42):
            with self.subTest(title=title):
                response = self.client.patch(
                    "/api/v1/threads/research-one", json={"title": title}
                )
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(session_store.get_thread("research-one")["title"], "原始标题")

    def test_delete_hides_thread_from_every_public_thread_endpoint(self):
        response = self.client.delete("/api/v1/threads/research-one")
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(response.content, b"")
        self.assertEqual(
            [row["thread_id"] for row in self.client.get("/api/v1/threads").json()["items"]],
            ["research-two"],
        )
        for path in ("", "/messages", "/status"):
            with self.subTest(path=path):
                response = self.client.get("/api/v1/threads/research-one" + path)
                self.assertEqual(response.status_code, 404, response.text)
        for path in ("/api/v1/chat", "/api/v1/chat/stream"):
            with self.subTest(path=path):
                response = self.client.post(path, json={"thread_id": "research-one", "message": "不得复活"})
                self.assertEqual(response.status_code, 404, response.text)
        review = {"decisions": {"call-one": "approve"}, "expected_fingerprints": {"call-one": "hash"}}
        for path in ("/review", "/review/stream"):
            with self.subTest(path=path):
                response = self.client.post("/api/v1/threads/research-one" + path, json=review)
                self.assertEqual(response.status_code, 404, response.text)

    def test_deleted_thread_cannot_replay_a_cached_chat_response(self):
        request_journal.initialize_request_journal()
        request_journal.register_request(
            request_id="req-thread-replay",
            thread_id="research-one",
            operation="chat",
            payload={"message": "原始问题"},
        )
        request_journal.start_request("req-thread-replay")
        request_journal.finish_request(
            "req-thread-replay", status="completed",
            response={"thread_id": "research-one", "status": "completed", "answer": "原始回答", "pending_approvals": []},
        )
        self.assertEqual(self.client.delete("/api/v1/threads/research-one").status_code, 204)
        for path in ("/api/v1/chat", "/api/v1/chat/stream"):
            with self.subTest(path=path):
                response = self.client.post(path, json={
                    "thread_id": "research-one", "message": "原始问题", "request_id": "req-thread-replay",
                })
                self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(request_journal.get_request("req-thread-replay")["status"], "completed")

    def test_unknown_or_deleted_thread_cannot_be_modified(self):
        for thread_id in ("missing", "research-one"):
            if thread_id == "research-one":
                self.assertEqual(self.client.delete("/api/v1/threads/research-one").status_code, 204)
            with self.subTest(thread_id=thread_id):
                response = self.client.patch(f"/api/v1/threads/{thread_id}", json={"title": "new"})
                self.assertEqual(response.status_code, 404, response.text)
                response = self.client.delete(f"/api/v1/threads/{thread_id}")
                self.assertEqual(response.status_code, 404, response.text)

    def test_delete_returns_conflict_while_agent_is_running(self):
        self.lock.acquire()
        try:
            response = self.client.delete("/api/v1/threads/research-one")
        finally:
            self.lock.release()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIsNotNone(session_store.get_thread("research-one"))


if __name__ == "__main__":
    unittest.main()
