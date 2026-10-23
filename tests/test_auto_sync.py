"""自动同步回归：临时 Storage、真实解析/分块，云端向量用离线替身。"""
import json
import os
from contextlib import nullcontext
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from app.rag import auto_sync
from app.rag import chunk_sync, document_sync, vector_sync
from app.rag.auto_sync import AutoSyncService, SyncPaths, run_sync, settings_from_env
from app.rag.sync_lock import SyncBusyError, writer_lock
from tests.test_vector_sync import DIM, MODEL, FakeCollection, FakeEmbedder


class SyncFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "Storage"
        self.root.mkdir()
        self.paths = SyncPaths(self.root)
        self.enterContext(patch("os.path.ismount", return_value=True))
        self.collection, self.embed = FakeCollection(), FakeEmbedder()
        self.clients = Mock(return_value=(MODEL, DIM, self.collection, self.embed))

    def write(self, text, name="notes/test.md"):
        file = self.root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
        return file

    def sync(self):
        return run_sync(self.paths, clients_factory=self.clients)


class PipelineTests(SyncFixture):
    def test_empty_storage_does_not_request_document_embeddings(self):
        result = self.sync()
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(self.embed.calls, [])

    def test_new_modified_and_unchanged_reuse_existing_caches(self):
        self.write("# 同步测试\n\n第一版支持自动同步。")
        self.write("# 私密资料\n\n禁止发送云端。", "KnowledgeBase/documents/Diary/24.7.16.md")
        first = self.sync()
        self.assertEqual(first["status"], "success", first)
        self.assertEqual(first["stages"]["documents"]["new"], 2)
        self.assertEqual(first["stages"]["vectors"]["excluded"], 1)
        self.assertEqual(first["stages"]["vectors"]["newly_embedded_chunks"], 1)
        old_ids = set(self.collection.items)
        calls = len(self.embed.calls)
        second = self.sync()
        self.assertEqual(second["status"], "success", second)
        self.assertEqual(second["stages"]["vectors"]["newly_embedded_chunks"], 0)
        self.assertEqual(len(self.embed.calls), calls)
        self.write("# 同步测试\n\n第二版已经自动更新。")
        third = self.sync()
        self.assertEqual(third["status"], "success", third)
        self.assertEqual(third["stages"]["documents"]["modified"], 1)
        self.assertTrue(old_ids.isdisjoint(self.collection.items))
        self.assertTrue(all("第二版" in item["content"] for item in self.collection.items.values()))

    def test_parse_failure_stops_before_cloud(self):
        file = self.root / "broken.docx"
        file.write_bytes(b"not a docx zip")
        result = self.sync()
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["stage"], "documents")
        self.clients.assert_not_called()
        self.assertFalse(self.paths.vector_db.exists())

    def test_embedding_failure_keeps_previous_active_version_and_can_retry(self):
        self.write("# 第一版\n\n保留有效的旧索引。")
        self.assertEqual(self.sync()["status"], "success")
        old_ids = set(self.collection.items)
        self.write("# 第二版\n\n等待网络恢复后重试。")
        self.embed.fail_after = 0
        failed = self.sync()
        self.assertEqual(failed["status"], "failed", failed)
        self.assertEqual(failed["stage"], "vectors")
        self.assertTrue(old_ids.issubset(self.collection.items))
        self.assertTrue(all(self.collection.items[key]["metadata"]["is_active"] for key in old_ids))
        self.embed.fail_after = None
        self.assertEqual(self.sync()["status"], "success")
        self.assertTrue(old_ids.isdisjoint(self.collection.items))

    def test_unmounted_storage_is_never_created_or_scanned(self):
        with patch("os.path.ismount", return_value=False):
            result = self.sync()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stage"], "mount")
        self.clients.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_writer_lock_excludes_other_process_and_is_released(self):
        code = """import sys
from pathlib import Path
from unittest.mock import patch
from app.rag.sync_lock import writer_lock, SyncBusyError
try:
    with patch('os.path.ismount', return_value=True), writer_lock(Path(sys.argv[1])):
        pass
except SyncBusyError:
    sys.exit(75)
"""
        with writer_lock(self.root):
            attempt = subprocess.run([sys.executable, "-c", code, str(self.root)], capture_output=True, timeout=10)
            self.assertEqual(attempt.returncode, 75, attempt.stderr.decode())
            with self.assertRaises(SyncBusyError):
                with writer_lock(self.root):
                    self.fail("第二个写入者获得了锁")
        with writer_lock(self.root):
            pass

    def test_manual_write_commands_obey_scheduler_ownership(self):
        for module, flag, function in (
            (document_sync, "--parse", "sync_storage"),
            (chunk_sync, "--sync", "sync_chunks"),
            (vector_sync, "--write", "_real_clients"),
        ):
            with self.subTest(module=module.__name__), writer_lock(self.root), \
                    patch.object(module, "STORAGE_ROOT", self.root), \
                    patch.object(module, function) as operation, \
                    patch.object(sys, "argv", [module.__name__, flag]):
                with self.assertRaises(SystemExit) as exc:
                    module.main()
                self.assertIn("已有同步写入者", str(exc.exception))
                operation.assert_not_called()

    def test_preflight_review_blocks_cloud_and_preserves_index(self):
        self.write("# 正常文档\n\n等待审核。")
        original = vector_sync.build_plan
        def require_review(**params):
            summary, plan = original(**params)
            summary["chunks_requiring_review"] = 1
            return summary, plan
        with patch.object(vector_sync, "build_plan", side_effect=require_review):
            result = self.sync()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stage"], "preflight")
        self.clients.assert_not_called()


class SchedulerTests(SyncFixture):
    def service(self, runner, **kwargs):
        service = AutoSyncService(paths=self.paths, interval_seconds=kwargs.pop("interval_seconds", 0.04),
                                  graph_lock=kwargs.pop("graph_lock", threading.Lock()),
                                  runner=runner, **kwargs)
        self.addCleanup(service.stop)
        return service

    def test_busy_agent_is_skipped_then_next_tick_runs_and_logs(self):
        lock, done = threading.Lock(), threading.Event()
        lock.acquire()
        self.addCleanup(lambda: lock.locked() and lock.release())
        service = self.service(lambda: done.set() or {"status": "success", "stages": {}}, graph_lock=lock)
        service.start()
        self.assertFalse(done.wait(0.1))
        lock.release()
        self.assertTrue(done.wait(2))
        service.stop()
        state = json.loads(self.paths.status_file.read_text())
        self.assertEqual(state["status"], "success", state)
        self.assertIsNotNone(state["last_success_at"])
        self.assertIn('"status": "success"', self.paths.log_file.read_text())
        self.assertEqual(self.paths.status_file.stat().st_mode & 0o777, 0o600)

    def test_failure_is_logged_and_next_tick_retries(self):
        done = threading.Event()
        calls = []
        def run():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("模拟任务异常")
            done.set()
            return {"status": "success", "stages": {}}
        service = self.service(run)
        service.start()
        self.assertTrue(done.wait(2))
        service.stop()
        log = self.paths.log_file.read_text()
        self.assertIn("模拟任务异常", log)
        self.assertIn('"status": "success"', log)

    def test_real_chroma_existing_reader_sees_background_updates(self):
        import chromadb
        from chromadb.config import Settings
        client = chromadb.PersistentClient(path=str(self.paths.kb / "chroma"),
                                          settings=Settings(anonymized_telemetry=False))
        collection = client.create_collection("auto_sync_test", embedding_function=None,
                                              metadata=vector_sync._collection_metadata(MODEL, DIM))
        self.clients.return_value = (MODEL, DIM, collection, self.embed)
        self.write("# 原始版本\n\n更新之前的内容。")
        first, modified = threading.Event(), threading.Event()
        def run():
            result = self.sync()
            if result["status"] == "success":
                first.set()
                if result["stages"]["vectors"]["modified"]:
                    modified.set()
            return result
        service = self.service(run)
        service.start()
        self.assertTrue(first.wait(5))
        before = collection.query(query_embeddings=[[1., 1., 2., 3.]], n_results=1)
        self.assertIn("更新之前", before["documents"][0][0])
        self.write("# 新版本\n\n更新之后的内容已经可见。")
        self.assertTrue(modified.wait(5))
        after = collection.query(query_embeddings=[[1., 1., 2., 3.]], n_results=1)
        self.assertIn("更新之后", after["documents"][0][0])
        self.assertNotEqual(before["ids"], after["ids"])
        service.stop()

    def test_stop_waits_for_inflight_sync_and_wakes_sleep_immediately(self):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def run():
            entered.set()
            release.wait(3)
            return {"status": "success"}
        service = self.service(run)
        service.start()
        self.assertTrue(entered.wait(2))
        stopper = threading.Thread(target=lambda: (service.stop(), stopped.set()))
        stopper.start()
        self.assertFalse(stopped.wait(0.05))
        release.set()
        self.assertTrue(stopped.wait(2))
        stopper.join()

    def test_only_one_service_owns_writer_lock(self):
        first_tick, second_tick = threading.Event(), threading.Event()
        first = self.service(lambda: first_tick.set() or {"status": "success"})
        second = self.service(lambda: second_tick.set() or {"status": "success"})
        first.start()
        self.assertTrue(first_tick.wait(2))
        second.start()
        self.assertFalse(second_tick.wait(0.15))
        first.stop()
        self.assertTrue(second_tick.wait(2))

    def test_publishing_or_logging_failure_does_not_release_ownership(self):
        for failing in ("_publish", "_log_handler"):
            with self.subTest(failing=failing):
                observed = threading.Event()
                service = self.service(lambda: {"status": "success"}, interval_seconds=10)
                with patch.object(auto_sync, failing, side_effect=PermissionError("模拟日志权限错误")), \
                        patch.object(auto_sync.logger, "exception", side_effect=lambda *a, **k: observed.set()):
                    service.start()
                    self.assertTrue(observed.wait(2))
                    try:
                        with self.assertRaises(SyncBusyError):
                            with writer_lock(self.root):
                                self.fail("日志异常使 API 丢失了索引所有权")
                    finally:
                        service.stop()

    def test_api_startup_requires_ownership_before_becoming_ready(self):
        runner = Mock(return_value={"status": "success"})
        service = self.service(runner)
        with writer_lock(self.root):
            with self.assertRaises(SyncBusyError):
                service.start(wait_for_ownership=True)
        runner.assert_not_called()

    def test_successful_api_startup_already_holds_ownership(self):
        service = self.service(lambda: {"status": "success"})
        service.start(wait_for_ownership=True)
        with self.assertRaises(SyncBusyError):
            with writer_lock(self.root):
                self.fail("API 就绪前未取得所有权")

    def test_disabled_service_does_not_touch_storage(self):
        runner = Mock()
        service = self.service(runner, enabled=False)
        service.start()
        service.stop()
        runner.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unmounted_scheduler_can_recover_without_creating_local_kb(self):
        mounted, done = threading.Event(), threading.Event()
        with patch("os.path.ismount", side_effect=lambda _: mounted.is_set()):
            service = self.service(lambda: done.set() or {"status": "success"})
            service.start()
            self.assertFalse(done.wait(0.1))
            self.assertEqual(list(self.root.iterdir()), [])
            mounted.set()
            self.assertTrue(done.wait(2))
            service.stop()

    def test_environment_settings_validate_boolean_and_interval(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings_from_env(), {"enabled": True, "interval_seconds": 86400})
        for enabled in ("0", "false", "off"):
            with patch.dict(os.environ, {"RAG_AUTO_SYNC_ENABLED": enabled, "RAG_AUTO_SYNC_INTERVAL_SECONDS": "30"}):
                self.assertEqual(settings_from_env(), {"enabled": False, "interval_seconds": 30})
        for value in ("0", "-1", "NaN", "oops"):
            with patch.dict(os.environ, {"RAG_AUTO_SYNC_INTERVAL_SECONDS": value}):
                with self.assertRaises(ValueError):
                    settings_from_env()


class ApiLifecycleTests(SyncFixture):
    def api(self):
        from app.api import main
        self.enterContext(patch.object(main.session_store, "initialize_database"))
        self.enterContext(patch.object(main.request_journal, "initialize_request_journal"))
        self.enterContext(patch.object(main, "open_checkpointer", side_effect=lambda: nullcontext(object())))
        self.enterContext(patch.object(main, "build_graph", return_value=object()))
        self.enterContext(patch.object(main, "settings_from_env", return_value={"enabled": True, "interval_seconds": 86400}))
        def create(**kwargs):
            self.service = AutoSyncService(paths=self.paths, runner=lambda: {"status": "success"}, **kwargs)
            return self.service
        self.enterContext(patch.object(main, "AutoSyncService", side_effect=create))
        return main.app

    def test_api_owns_index_before_ready_and_releases_after_shutdown(self):
        from fastapi.testclient import TestClient
        app = self.api()
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/v1/health").status_code, 200)
            with self.assertRaises(SyncBusyError):
                with writer_lock(self.root):
                    self.fail("已就绪的 API 尚未保护索引")
        self.assertFalse(self.service._thread.is_alive())
        with writer_lock(self.root):
            pass

    def test_api_refuses_startup_while_external_writer_is_active(self):
        from fastapi.testclient import TestClient
        app = self.api()
        with writer_lock(self.root):
            with self.assertRaises(SyncBusyError):
                with TestClient(app):
                    self.fail("外部写入尚未完成时 API 已就绪")
        self.assertFalse(self.service._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
