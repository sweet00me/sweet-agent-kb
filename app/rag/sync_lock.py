"""Storage 写入者的跨进程互斥；API 定时任务在生命周期内持有此锁。"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path


class SyncBusyError(RuntimeError):
    pass


class StorageUnavailableError(RuntimeError):
    pass


@contextmanager
def writer_lock(storage_root: Path):
    """禁止并行写入同一个本地 Chroma；未挂载时不创建目录。"""
    if not os.path.ismount(storage_root):
        raise StorageUnavailableError("Storage 未挂载，自动同步将在下个周期重试")
    directory = Path(storage_root) / "KnowledgeBase/sqlite"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(directory / "rag_sync.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncBusyError("已有同步写入者；API 自动同步运行期间，请由 API 统一更新索引") from exc
        yield
    finally:
        os.close(descriptor)


@contextmanager
def cli_writer_lock(storage_root: Path, *, write: bool):
    """手动命令保持只读预览；实际写入必须遵守同一个所有者锁。"""
    if not write:
        yield
        return
    try:
        with writer_lock(storage_root):
            yield
    except (SyncBusyError, StorageUnavailableError) as exc:
        raise SystemExit(str(exc)) from exc
