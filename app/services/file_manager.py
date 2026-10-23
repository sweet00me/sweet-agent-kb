
"""
文件：app/services/file_manager.py

用途：
    提供 Linux 文件夹安全浏览能力。

主要功能：
    1. 根据 root_id 获取已授权存储入口。
    2. 使用相对路径进入子目录。
    3. 返回文件和目录元数据。
    4. 支持排序、分页和返回上级目录。
    5. 阻止路径穿越、隐藏路径及符号链接访问。

安全设计：
    使用 Linux openat 风格的 dir_fd 逐级打开目录。
    不跟随符号链接。
    不读取文件正文。
    不修改文件系统。
"""

import os
import stat

from contextlib import ExitStack
from datetime import datetime, timezone

from app.services.storage import list_storage_roots


MAX_DIRECTORY_ENTRIES = 20000
MAX_PATH_LENGTH = 1024

# Linux 平台下拒绝跟随符号链接。
DIRECTORY_FLAGS = (
    os.O_RDONLY
    | os.O_DIRECTORY
    | os.O_NOFOLLOW
    | os.O_CLOEXEC
)

SENSITIVE_NAMES = {
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    "authorized_keys",
    "credentials.json",
}


class FileManagerError(Exception):
    """文件管理服务基础异常。"""


class UnknownRootError(FileManagerError):
    """存储入口不存在。"""


class InvalidPathError(FileManagerError):
    """相对路径不合法。"""


class DirectoryUnavailableError(FileManagerError):
    """目录不存在或不可安全访问。"""


class DirectoryTooLargeError(FileManagerError):
    """目录条目数量超出第一版支持范围。"""


# =====================================
# 1. 检查文件名
# =====================================

def is_visible_name(name: str) -> bool:
    """
    过滤隐藏文件、已知敏感名称和控制字符。

    该规则仅适用于初期目录浏览。
    不能替代后续下载接口的完整授权检查。
    """

    if not name or name.startswith("."):
        return False

    if name.lower() in SENSITIVE_NAMES:
        return False

    if any(ord(char) < 32 for char in name):
        return False

    return True


# =====================================
# 2. 验证目录相对路径
# =====================================

def validate_relative_path(
    relative_path: str,
) -> list[str]:
    """
    允许：
        ""
        Workspace
        Workspace/sweet-agent-kb
        Storage/KnowledgeBase

    拒绝：
        /etc
        ../
        Workspace/../../etc
        .ssh
        Workspace/.git
        使用反斜杠或空路径段的路径
    """

    if not isinstance(relative_path, str):
        raise InvalidPathError("路径必须是字符串")

    if len(relative_path) > MAX_PATH_LENGTH:
        raise InvalidPathError("路径过长")

    if relative_path == "":
        return []

    if (
        relative_path.startswith("/")
        or "\\" in relative_path
        or "\x00" in relative_path
    ):
        raise InvalidPathError("路径格式不合法")

    parts = relative_path.split("/")

    for part in parts:

        if part in ("", ".", ".."):
            raise InvalidPathError(
                "不允许空路径段或路径穿越"
            )

        if not is_visible_name(part):
            raise InvalidPathError(
                "不允许访问隐藏或受保护路径"
            )

        if len(os.fsencode(part)) > 255:
            raise InvalidPathError(
                "文件名过长"
            )

    return parts


# =====================================
# 3. 根据 root_id 查找存储根目录
# =====================================

def get_storage_root(root_id: str) -> dict:
    """
    root_id 必须来自 storage.py 实时生成的列表。

    客户端不能通过 root_id 自行指定绝对路径。
    """

    for root in list_storage_roots():

        if root["root_id"] == root_id:
            return root

    raise UnknownRootError(
        "存储入口不存在或已经卸载"
    )


# =====================================
# 4. 安全打开目录
# =====================================

def open_directory(
    root_path: str,
    parts: list[str],
    stack: ExitStack,
) -> int:
    """
    从可信根目录开始，使用文件描述符逐级进入。

    每一级都使用 O_DIRECTORY | O_NOFOLLOW，
    避免通过目录符号链接逃离授权根目录。

    ExitStack 负责统一关闭文件描述符。
    """

    fd = os.open(
        root_path,
        DIRECTORY_FLAGS,
    )

    stack.callback(os.close, fd)

    for part in parts:

        next_fd = os.open(
            part,
            DIRECTORY_FLAGS,
            dir_fd=fd,
        )

        stack.callback(os.close, next_fd)

        fd = next_fd

    return fd


# =====================================
# 5. 读取文件夹
# =====================================

def list_directory(
    root_id: str,
    relative_path: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """
    查询指定文件夹中的直接子项。

    不递归扫描子目录，不读取文件内容。

    第一版采用排序后 offset 分页。
    单个目录最多扫描 20000 个条目。
    后续可以升级为适合大型目录的游标分页。
    """

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 200
    ):
        raise ValueError(
            "limit 必须在 1～200 之间"
        )

    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
    ):
        raise ValueError(
            "offset 必须为非负整数"
        )

    parts = validate_relative_path(
        relative_path
    )

    root = get_storage_root(root_id)

    items = []

    try:

        with ExitStack() as stack:

            directory_fd = open_directory(
                root["path"],
                parts,
                stack,
            )

            with os.scandir(directory_fd) as entries:

                for entry in entries:

                    if len(items) >= MAX_DIRECTORY_ENTRIES:
                        raise DirectoryTooLargeError(
                            "目录条目超过当前支持上限"
                        )

                    if not is_visible_name(
                        entry.name
                    ):
                        continue

                    try:
                        info = entry.stat(
                            follow_symlinks=False
                        )
                    except OSError:
                        # 文件可能在扫描过程中被删除。
                        continue

                    mode = info.st_mode

                    if stat.S_ISDIR(mode):
                        kind = "directory"
                        size_bytes = None

                    elif stat.S_ISREG(mode):
                        kind = "file"
                        size_bytes = info.st_size

                    else:
                        # 排除符号链接、设备、Socket 等。
                        continue

                    child_path = "/".join(
                        parts + [entry.name]
                    )

                    items.append({
                        "name": entry.name,
                        "path": child_path,
                        "kind": kind,
                        "size_bytes": size_bytes,
                        "modified_at": (
                            datetime.fromtimestamp(
                                info.st_mtime,
                                timezone.utc,
                            ).isoformat()
                        ),
                    })

    except DirectoryTooLargeError:
        raise

    except OSError as exc:
        raise DirectoryUnavailableError(
            "目录不存在、权限不足或路径不安全"
        ) from exc

    # 文件夹优先，其次按名称排序。
    items.sort(
        key=lambda item: (
            item["kind"] != "directory",
            item["name"].casefold(),
            item["name"],
        )
    )

    total = len(items)

    page = items[
        offset:offset + limit
    ]

    parent_path = (
        "/".join(parts[:-1])
        if parts
        else None
    )

    return {
        "root_id": root_id,
        "path": relative_path,
        "parent_path": parent_path,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
        "items": page,
    }
