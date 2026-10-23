"""复用文件管理器的 openat 校验，安全读取来源和解析缓存。"""
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import sqlite3
from urllib.parse import quote

from app.services.file_manager import DIRECTORY_FLAGS, InvalidPathError, open_directory, validate_relative_path


class AnalysisError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def checked_path(value: str, *, allow_root=False) -> str:
    try:
        parts = validate_relative_path(value)
    except (InvalidPathError, TypeError, ValueError) as exc:
        raise AnalysisError("invalid_path") from exc
    if not parts and not allow_root:
        raise AnalysisError("invalid_path")
    return "/".join(parts)


@contextmanager
def open_regular(root: Path, relative: str, maximum: int):
    """每一级拒绝链接；仅打开同设备普通文件，非阻塞避免 FIFO 挂起。"""
    parts = checked_path(relative).split("/")
    try:
        with ExitStack() as stack:
            parent = open_directory(str(root), parts[:-1], stack)
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                 dir_fd=parent)
            stack.callback(os.close, descriptor)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_dev != root.stat().st_dev:
                raise AnalysisError("access_denied")
            if info.st_size > maximum:
                raise AnalysisError("file_too_large")
            yield descriptor, info
    except FileNotFoundError as exc:
        raise AnalysisError("file_missing") from exc
    except OSError as exc:
        raise AnalysisError("access_denied") from exc


def read_checked(root: Path, relative: str, maximum: int, *, keep_bytes=False):
    digest, pieces, length = hashlib.sha256(), [], 0
    with open_regular(root, relative, maximum) as (descriptor, info):
        while True:
            block = os.read(descriptor, min(65536, maximum + 1 - length))
            if not block:
                break
            length += len(block)
            if length > maximum:
                raise AnalysisError("file_too_large")
            digest.update(block)
            if keep_bytes:
                pieces.append(block)
        after = os.fstat(descriptor)
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise AnalysisError("source_changed")
    return digest.hexdigest(), b"".join(pieces) if keep_bytes else None, info


def database_relative(root: Path, path: Path) -> str:
    try:
        return checked_path(path.relative_to(root).as_posix())
    except (ValueError, AnalysisError) as exc:
        raise AnalysisError("invalid_database_path") from exc


@contextmanager
def exclusive_analysis_lock(root: Path, path: Path):
    """Map 与派生分析共用的安全、非阻塞进程锁；目录必须已经存在。"""
    parts = database_relative(root, path).split("/")
    try:
        with ExitStack() as stack:
            parent = open_directory(str(root), parts[:-1], stack)
            descriptor = os.open(parts[-1], os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                                 0o600, dir_fd=parent)
            stack.callback(os.close, descriptor)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AnalysisError("invalid_analysis_lock")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AnalysisError("analysis_busy") from exc
            yield
    except OSError as exc:
        raise AnalysisError("analysis_lock_unavailable") from exc


@contextmanager
def database_connection(root: Path, path: Path, *, write=False, create=False, forbidden: Path | None = None):
    """固定目录描述符入口，连接前后核对身份，拒绝链接与已有状态库别名。"""
    parts = database_relative(root, path).split("/")
    try:
        with ExitStack() as stack:
            parent = open_directory(str(root), [], stack)
            parents = [parent]
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                parent = os.open(part, DIRECTORY_FLAGS, dir_fd=parent)
                stack.callback(os.close, parent)
                parents.append(parent)
            flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            if create:
                try:
                    descriptor = os.open(parts[-1], flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
                except FileExistsError:
                    descriptor = os.open(parts[-1], flags, dir_fd=parent)
            else:
                descriptor = os.open(parts[-1], flags, dir_fd=parent)
            stack.callback(os.close, descriptor)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != os.fstat(parents[0]).st_dev:
                raise AnalysisError("invalid_database_path")
            if forbidden is not None:
                relative = database_relative(root, forbidden)
                with open_regular(root, relative, 2**63 - 1) as (_, other):
                    if (info.st_dev, info.st_ino) == (other.st_dev, other.st_ino):
                        raise AnalysisError("database_alias")
            parent_versions = [(fd, os.fstat(fd).st_ctime_ns) for fd in parents]
            uri = f"file:/proc/self/fd/{parent}/{quote(parts[-1], safe='')}?mode={'rw' if write else 'ro'}"
            connection = sqlite3.connect(uri, uri=True, timeout=10)
            stack.callback(connection.close)
            current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            signature = lambda item: (item.st_dev, item.st_ino, item.st_nlink, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
            if signature(current) != signature(info) or signature(os.fstat(descriptor)) != signature(info):
                raise AnalysisError("database_changed")
            if any(os.fstat(fd).st_ctime_ns != version for fd, version in parent_versions):
                raise AnalysisError("database_changed")
            actual = Path(connection.execute("PRAGMA database_list").fetchone()[2])
            actual_info = actual.stat(follow_symlinks=False)
            if (actual_info.st_dev, actual_info.st_ino) != (info.st_dev, info.st_ino):
                raise AnalysisError("database_changed")
            yield connection
    except (OSError, sqlite3.Error) as exc:
        raise AnalysisError("database_unavailable") from exc
