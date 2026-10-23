"""随 API 启动的定时增量同步；与检索共享同一个 Chroma 进程。"""
import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import threading
import time

from app.rag import chunk_sync, document_sync, vector_sync
from app.rag.sync_lock import StorageUnavailableError, SyncBusyError, writer_lock

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncPaths:
    storage_root: Path = document_sync.STORAGE_ROOT

    @property
    def kb(self):
        return self.storage_root / "KnowledgeBase"

    @property
    def vector_db(self):
        return self.kb / "sqlite/vector_sync.sqlite3"

    @property
    def log_file(self):
        return self.kb / "logs/auto_sync.log"

    @property
    def status_file(self):
        return self.kb / "logs/auto_sync_status.json"


def settings_from_env() -> dict:
    enabled = os.environ.get("RAG_AUTO_SYNC_ENABLED", "1").strip().lower()
    if enabled not in {"1", "true", "on", "0", "false", "off"}:
        raise ValueError("RAG_AUTO_SYNC_ENABLED 必须为 1/0、true/false 或 on/off")
    interval = int(os.environ.get("RAG_AUTO_SYNC_INTERVAL_SECONDS", "86400"))
    if interval <= 0:
        raise ValueError("RAG_AUTO_SYNC_INTERVAL_SECONDS 必须为正整数")
    return {"enabled": enabled in {"1", "true", "on"}, "interval_seconds": interval}


def _now():
    return datetime.now(timezone.utc).isoformat()


def run_sync(paths: SyncPaths | None = None, *, clients_factory=None) -> dict:
    """执行一次完整管线；调用方必须持有 writer_lock，避免进程间写入。"""
    paths = paths or SyncPaths()
    started = time.monotonic()
    result = {"status": "running", "started_at": _now(), "stage": "mount", "stages": {}}
    try:
        if not os.path.ismount(paths.storage_root):
            raise StorageUnavailableError("Storage 未挂载，未执行扫描或写入")
        result["stage"] = "documents"
        documents = document_sync.sync_storage(
            paths.storage_root, paths.kb / "parsed_documents",
            paths.kb / "sqlite/document_sync.sqlite3", commit=True,
        )
        result["stages"]["documents"] = documents
        if documents["failed"]:
            raise RuntimeError("文档解析失败，未进入分块或向量阶段")

        result["stage"] = "chunks"
        # 空知识库尚无解析文件时，也为现有分块器提供合法的缓存根目录。
        (paths.kb / "parsed_documents").mkdir(parents=True, exist_ok=True, mode=0o700)
        chunks = chunk_sync.sync_chunks(
            source_root=paths.storage_root, parsed_root=paths.kb / "parsed_documents",
            source_db=paths.kb / "sqlite/document_sync.sqlite3",
            cache_dir=paths.kb / "chunk_cache", chunk_db=paths.kb / "sqlite/chunk_sync.sqlite3", write=True,
        )
        result["stages"]["chunks"] = chunks
        if chunks["failed"] or chunks["pending_parse"]:
            raise RuntimeError("分块失败或原件再次变化，未调用云端 Embedding")

        params = dict(source_root=paths.storage_root, chunk_dir=paths.kb / "chunk_cache",
                      chunk_db=paths.kb / "sqlite/chunk_sync.sqlite3", vector_db=paths.vector_db)
        result["stage"] = "preflight"
        preflight, _ = vector_sync.build_plan(**params)
        result["stages"]["preflight"] = preflight
        if preflight["failed"] or preflight["pending_parse"] or preflight["chunks_requiring_review"]:
            raise RuntimeError("向量预检查未通过，保留现有索引且未调用云端 Embedding")

        result["stage"] = "vectors"
        model, dimensions, collection, embed = (clients_factory or vector_sync._real_clients)()
        vectors = vector_sync.sync_vectors(**params, write=True, model=model, dimensions=dimensions,
                                          collection=collection, embed_texts=embed)
        result["stages"]["vectors"] = vectors
        if vectors["failed"] or vectors["pending_parse"]:
            raise RuntimeError("向量同步未全部成功，下个周期重试；旧有效版本由现有写入保护保留")
        result["status"] = "success"
        result["stage"] = "completed"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["finished_at"] = _now()
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result


def _log_handler(paths):
    paths.log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(paths.log_file, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    handler = RotatingFileHandler(paths.log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    return handler


def _publish(paths, report, interval_seconds, last_success, run_logger):
    if not os.path.ismount(paths.storage_root):
        logger.warning("Storage 未挂载，跳过同步状态文件写入")
        return last_success
    if report["status"] == "success":
        last_success = report.get("finished_at") or _now()
    next_run = (datetime.now(timezone.utc) + timedelta(seconds=interval_seconds)).isoformat() if interval_seconds else None
    report = dict(report, last_success_at=last_success, interval_seconds=interval_seconds, next_run_at=next_run)
    document_sync.save_json_atomic(paths.status_file, report)
    level = logging.ERROR if report["status"] == "failed" else logging.INFO
    run_logger.log(level, "%s", json.dumps(report, ensure_ascii=False))
    return last_success


class AutoSyncService:
    def __init__(self, *, graph_lock, paths=None, interval_seconds=86400, enabled=True, runner=None):
        self.paths = paths or SyncPaths()
        self.interval_seconds = interval_seconds
        self.enabled = enabled
        self.graph_lock = graph_lock
        self.runner = runner or (lambda: run_sync(self.paths))
        self._stop = threading.Event()
        self._startup = threading.Event()
        self._startup_error = None
        self._thread = None

    def start(self, *, wait_for_ownership=False):
        if self.enabled and self._thread is None:
            self._thread = threading.Thread(target=self._serve, name="sweet-rag-auto-sync", daemon=True)
            self._thread.start()
            if wait_for_ownership:
                self._startup.wait()
                if self._startup_error is not None:
                    self.stop()
                    raise self._startup_error

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            # 等待本轮原子写入结束，再由 API 关闭 Checkpointer 等资源。
            self._thread.join()

    def _serve(self):
        while not self._stop.is_set():
            try:
                # 生命周期内占有写入权：外部进程更新会使嵌入式 Chroma 的查询缓存过期。
                with writer_lock(self.paths.storage_root):
                    self._signal_startup()
                    while not self._stop.is_set():
                        try:
                            self._serve_owned()
                        except Exception:
                            # 状态/日志失败也必须继续占有锁，API 的查询缓存仍然存活。
                            logger.exception("自动同步记录异常，保留索引所有权并在下个周期重试")
                        self._stop.wait(self.interval_seconds)
            except (StorageUnavailableError, SyncBusyError) as exc:
                self._signal_startup(exc)
                logger.warning("自动同步暂缓：%s", exc)
            except Exception as exc:
                self._signal_startup(exc)
                logger.exception("自动同步异常，将于下个周期重试")
            self._stop.wait(self.interval_seconds)

    def _signal_startup(self, error=None):
        if not self._startup.is_set():
            self._startup_error = error
            self._startup.set()

    def _serve_owned(self):
        run_logger = logging.getLogger(f"{__name__}.{id(self)}")
        run_logger.setLevel(logging.INFO)
        handler = _log_handler(self.paths)
        run_logger.addHandler(handler)
        last_success = None
        try:
            try:
                last_success = json.loads(self.paths.status_file.read_text())["last_success_at"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
            while not self._stop.is_set():
                if not self.graph_lock.acquire(blocking=False):
                    report = {"status": "skipped_busy", "finished_at": _now(), "reason": "Agent 正在执行，下个周期重试"}
                else:
                    try:
                        report = self.runner()
                    except Exception as exc:
                        report = {"status": "failed", "finished_at": _now(), "error": f"{type(exc).__name__}: {exc}"}
                    finally:
                        self.graph_lock.release()
                last_success = _publish(self.paths, report, self.interval_seconds, last_success, run_logger)
                self._stop.wait(self.interval_seconds)
        finally:
            run_logger.removeHandler(handler)
            handler.close()


def main():
    parser = argparse.ArgumentParser(description="一次完成 Storage 解析、分块、Embedding 和 Chroma 同步（会写入并调用云端）")
    parser.add_argument("--once", action="store_true", required=True, help="执行一次；API 自动同步运行时拒绝外部写入")
    parser.parse_args()
    os.umask(0o077)
    paths = SyncPaths()
    try:
        with writer_lock(paths.storage_root):
            handler = _log_handler(paths)
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            try:
                report = run_sync(paths)
                _publish(paths, report, 0, None, logger)
                print(json.dumps(report, ensure_ascii=False, indent=2))
            finally:
                logger.removeHandler(handler)
                handler.close()
    except (StorageUnavailableError, SyncBusyError) as exc:
        raise SystemExit(str(exc)) from exc
    if report["status"] != "success":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
