
"""
文件：app/services/file_transfer.py

用途：
    为 Sweet Research Agent 提供安全的文件传输服务。

当前功能：
    1. 根据 root_id 与相对路径打开文件。
    2. 防止路径穿越和符号链接访问。
    3. 只允许下载普通文件。
    4. 使用流式读取，避免把完整文件载入内存。
    5. 对文件描述符进行统一关闭。

安全说明：
    不允许任意绝对路径。
    不执行 Shell 命令。
    不修改原始文件。
"""

import os
import stat

from contextlib import ExitStack
from dataclasses import dataclass

from app.services.file_manager import (
    get_storage_root,
    validate_relative_path,
    open_directory,
    InvalidPathError,
)


CHUNK_SIZE = 64 * 1024

# 第一版单文件最大 4 GiB。
# 流式传输不代表没有带宽和连接资源消耗。
MAX_DOWNLOAD_BYTES = 4 * 1024**3


class DownloadAccessError(Exception):
    """文件不能安全访问。"""


class DownloadNotFoundError(Exception):
    """下载文件不存在。"""


class DownloadTooLargeError(Exception):
    """文件超过当前下载大小上限。"""


@dataclass
class DownloadHandle:
    """
    已经安全打开的普通文件。

    通过文件描述符读取，避免检查路径后
    再次用原始路径打开导致的竞态问题。
    """

    fd: int
    filename: str
    size_bytes: int
    _closed: bool = False

    def close(self):
        """确保文件描述符只关闭一次。"""

        if not self._closed:
            self._closed = True
            os.close(self.fd)


# =====================================
# 1. 安全打开下载文件
# =====================================

def open_download(
    root_id: str,
    relative_path: str,
) -> DownloadHandle:
    """
    安全打开指定普通文件。

    参数：
        root_id：合法的存储入口 ID。
        relative_path：相对于存储根目录的文件路径。

    不允许：
        空路径、绝对路径、隐藏目录、
        路径穿越、符号链接、设备文件。
    """

    parts = validate_relative_path(
        relative_path
    )

    if not parts:
        raise InvalidPathError(
            "必须指定要下载的文件"
        )

    root = get_storage_root(root_id)

    filename = parts[-1]

    # 使用受控目录描述符定位文件。
    with ExitStack() as stack:

        try:
            directory_fd = open_directory(
                root["path"],
                parts[:-1],
                stack,
            )

            flags = (
                os.O_RDONLY
                | os.O_NOFOLLOW
                | os.O_CLOEXEC
                | os.O_NONBLOCK
            )

            file_fd = os.open(
                filename,
                flags,
                dir_fd=directory_fd,
            )

        except FileNotFoundError as exc:
            raise DownloadNotFoundError(
                "文件不存在"
            ) from exc

        except OSError as exc:
            raise DownloadAccessError(
                "文件无法安全打开"
            ) from exc

        try:
            info = os.fstat(file_fd)

            if not stat.S_ISREG(info.st_mode):
                raise DownloadAccessError(
                    "只允许下载普通文件"
                )

            if info.st_size > MAX_DOWNLOAD_BYTES:
                raise DownloadTooLargeError(
                    "文件超过下载大小上限"
                )

            return DownloadHandle(
                fd=file_fd,
                filename=filename,
                size_bytes=info.st_size,
            )

        except Exception:
            os.close(file_fd)
            raise


# =====================================
# 2. 流式读取文件
# =====================================

def iter_download_chunks(
    handle: DownloadHandle,
):
    """
    以固定大小分块读取已打开的文件。

    不重新解析用户输入的文件路径。
    不将全部内容加载到内存。
    """

    try:
        remaining = handle.size_bytes

        while remaining > 0:

            chunk = os.read(
                handle.fd,
                min(CHUNK_SIZE, remaining),
            )

            if not chunk:
                break

            remaining -= len(chunk)

            yield chunk

    finally:
        handle.close()
