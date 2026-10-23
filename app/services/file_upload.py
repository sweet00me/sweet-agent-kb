
"""
文件：app/services/file_upload.py

用途：
    为 Sweet Research Agent 提供安全的文件上传服务。

主要功能：
    1. 接收上传的科研文档和普通文件。
    2. 限制上传目录、文件类型与文件大小。
    3. 分块写入服务器磁盘。
    4. 临时文件写入完成后原子发布。
    5. 防止同名覆盖和符号链接路径访问。

当前限制：
    文件仅保存到 Storage/PC_Files/Uploads。
    不允许覆盖、删除或执行上传文件。
"""

import os
import secrets
from pathlib import Path
from contextlib import ExitStack

from app.services.storage import list_storage_roots
from app.services.file_manager import (
    open_directory,
    validate_relative_path,
    InvalidPathError,
)


STORAGE_ROOT = Path("/home/sweet/Storage")
UPLOAD_PARTS = ["PC_Files", "Uploads"]

UPLOAD_CHUNK_SIZE = 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

ALLOWED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".xlsx",
    ".csv",
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".png",
    ".jpg",
    ".jpeg",
}


class InvalidUploadError(Exception):
    """上传文件名或类型不合法。"""


class UploadTooLargeError(Exception):
    """上传文件超过大小限制。"""


class UploadConflictError(Exception):
    """目标文件名已存在。"""


class UploadUnavailableError(Exception):
    """上传目录不可用或磁盘写入失败。"""


# =====================================
# 1. 检查上传文件名
# =====================================

def validate_upload_filename(filename: str) -> str:
    """
    仅允许普通文件名，不允许目录路径。

    使用原有的路径安全校验规则，
    并限制可上传的文件扩展名。
    """

    if not isinstance(filename, str):
        raise InvalidUploadError("文件名无效")

    if (
        not filename
        or len(os.fsencode(filename)) > 200
    ):
        raise InvalidUploadError(
            "文件名为空或过长"
        )

    try:
        parts = validate_relative_path(filename)
    except InvalidPathError as exc:
        raise InvalidUploadError(
            "不允许上传隐藏文件或包含路径的文件名"
        ) from exc

    if len(parts) != 1:
        raise InvalidUploadError(
            "文件名不能包含目录"
        )

    extension = Path(filename).suffix.casefold()

    if extension not in ALLOWED_EXTENSIONS:
        raise InvalidUploadError(
            "暂不支持该文件类型"
        )

    return filename


# =====================================
# 2. 查找受控上传磁盘
# =====================================

def get_upload_root() -> dict:
    """
    只允许使用已经挂载并被识别的 Storage。

    不依赖客户端传入的任意 root_id，
    避免向 Workspace 或系统目录任意写入。
    """

    for root in list_storage_roots():

        if (
            root["kind"] == "mount"
            and root["path"] == str(STORAGE_ROOT)
            and root["writable"]
        ):
            return root

    raise UploadUnavailableError(
        "Storage 未挂载或不可写"
    )


# =====================================
# 3. 安全保存上传文件
# =====================================

def save_uploaded_file(
    filename: str,
    source,
) -> dict:
    """
    将上传文件保存到固定收件箱。

    source：
        支持 read(size) 的二进制文件对象。

    关键流程：
        1. 验证文件名。
        2. 安全打开上传目录。
        3. 创建随机临时文件。
        4. 分块写入并限制大小。
        5. 使用硬链接原子发布最终文件名。
        6. 清理临时文件。

    目标目录位于 ext4 Storage 磁盘。
    """

    filename = validate_upload_filename(filename)
    root = get_upload_root()

    try:

        with ExitStack() as stack:

            directory_fd = open_directory(
                root["path"],
                UPLOAD_PARTS,
                stack,
            )

            # 随机临时文件名不会覆盖已有上传。
            temp_name = (
                ".incoming-"
                + secrets.token_hex(16)
            )

            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_CLOEXEC
            )

            temp_fd = os.open(
                temp_name,
                flags,
                0o600,
                dir_fd=directory_fd,
            )

            total_bytes = 0

            try:

                # 使用 with 保证写入异常时关闭文件。
                with os.fdopen(temp_fd, "wb") as target:

                    while True:

                        chunk = source.read(
                            UPLOAD_CHUNK_SIZE
                        )

                        if not chunk:
                            break

                        total_bytes += len(chunk)

                        if total_bytes > MAX_UPLOAD_BYTES:
                            raise UploadTooLargeError(
                                "文件超过 50 MiB 上传限制"
                            )

                        target.write(chunk)

                    if total_bytes == 0:
                        raise InvalidUploadError(
                            "不允许上传空文件"
                        )

                    target.flush()
                    os.fsync(target.fileno())

                # =============================
                # 原子发布，不覆盖已有文件
                # =============================

                # 在相同 ext4 目录内创建最终文件名。
                # 若目标已经存在，os.link 会失败，
                # 不会覆盖任何原有内容。
                try:
                    os.link(
                        temp_name,
                        filename,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )

                except FileExistsError as exc:
                    raise UploadConflictError(
                        "同名文件已经存在"
                    ) from exc

                # 最终文件发布后删除临时名称。
                os.unlink(
                    temp_name,
                    dir_fd=directory_fd,
                )

                temp_name = None

                # 尽可能持久化目录元数据。
                os.fsync(directory_fd)

                return {
                    "status": "uploaded",
                    "root_id": root["root_id"],
                    "filename": filename,
                    "path": "/".join(
                        UPLOAD_PARTS + [filename]
                    ),
                    "size_bytes": total_bytes,
                }

            finally:

                # 发生文件过大、重名等异常时，
                # 删除尚未发布的临时文件。
                if temp_name is not None:
                    try:
                        os.unlink(
                            temp_name,
                            dir_fd=directory_fd,
                        )
                    except FileNotFoundError:
                        pass

    except (
        InvalidUploadError,
        UploadTooLargeError,
        UploadConflictError,
        UploadUnavailableError,
    ):
        raise

    except OSError as exc:
        raise UploadUnavailableError(
            "上传目录不可访问或磁盘写入失败"
        ) from exc
