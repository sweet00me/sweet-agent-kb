
"""
文件：app/services/file_search.py

用途：
    为 Sweet Research Agent 提供安全的文件名搜索功能。

主要功能：
    1. 按文件名关键词搜索。
    2. 支持当前目录与递归搜索。
    3. 支持中文与大小写不敏感匹配。
    4. 限制扫描数量、深度和运行时间。
    5. 不访问隐藏路径，不跟随符号链接。

安全说明：
    只读取文件元数据，不读取文件正文。
    不执行 Shell 命令。
    不修改任何文件。
    不递归进入其他文件系统挂载点。
"""

import os
import stat
import time

from collections import deque
from contextlib import ExitStack
from datetime import datetime, timezone

from app.services.file_manager import (
    get_storage_root,
    validate_relative_path,
    open_directory,
    is_visible_name,
    DirectoryUnavailableError,
)


# =====================================
# 1. 搜索资源限制
# =====================================

MAX_SCANNED_ENTRIES = 10000
MAX_SEARCH_SECONDS = 3.0
MAX_SEARCH_DEPTH = 8
MAX_SEARCH_RESULTS = 100


class InvalidSearchQueryError(Exception):
    """文件名搜索参数不合法。"""


# =====================================
# 2. 参数验证
# =====================================

def validate_search_query(query: str) -> str:
    """
    验证文件名搜索关键词。

    不允许空白关键词或控制字符。
    """

    if not isinstance(query, str):
        raise InvalidSearchQueryError(
            "搜索关键词必须是字符串"
        )

    query = query.strip()

    if not query or len(query) > 100:
        raise InvalidSearchQueryError(
            "搜索关键词长度必须为 1～100"
        )

    if any(ord(ch) < 32 for ch in query):
        raise InvalidSearchQueryError(
            "搜索关键词包含非法控制字符"
        )

    return query


# =====================================
# 3. 文件名搜索
# =====================================

def search_files(
    root_id: str,
    query: str,
    relative_path: str = "",
    recursive: bool = True,
    limit: int = 50,
    max_depth: int = 5,
) -> dict:
    """
    搜索指定存储入口中的文件名称。

    root_id：
        已授权的存储入口。

    relative_path：
        起始目录，相对于 root_id。

    recursive：
        是否搜索子文件夹。

    limit：
        最多返回多少个匹配结果。

    max_depth：
        相对于起始目录的递归深度。

    资源限制：
        最多检查 10000 个目录条目。
        最多执行约 3 秒。
        最多返回 100 个匹配结果。

    结果未必覆盖全部文件。
    incomplete=True 表示应缩小搜索范围。
    """

    query = validate_search_query(query)

    if not isinstance(recursive, bool):
        raise ValueError(
            "recursive 必须是布尔值"
        )

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_SEARCH_RESULTS
    ):
        raise ValueError(
            "limit 必须在 1～100 之间"
        )

    if (
        isinstance(max_depth, bool)
        or not isinstance(max_depth, int)
        or not 0 <= max_depth <= MAX_SEARCH_DEPTH
    ):
        raise ValueError(
            "max_depth 必须在 0～8 之间"
        )

    # 使用现有模块的路径安全检查。
    base_parts = validate_relative_path(
        relative_path
    )

    root = get_storage_root(root_id)

    keyword = query.casefold()

    # =====================================
    # 4. 检查起始目录
    # =====================================

    try:
        with ExitStack() as stack:

            directory_fd = open_directory(
                root["path"],
                base_parts,
                stack,
            )

            root_device = os.fstat(
                directory_fd
            ).st_dev

    except OSError as exc:
        raise DirectoryUnavailableError(
            "搜索起始目录不可访问"
        ) from exc

    # =====================================
    # 5. 初始化搜索队列
    # =====================================

    queue = deque([
        (base_parts, 0)
    ])

    results = []

    scanned_entries = 0
    skipped_directories = 0

    depth_limited = False
    stop_reason = None

    started_at = time.monotonic()

    # =====================================
    # 6. 开始递归搜索
    # =====================================

    while queue:

        if (
            time.monotonic() - started_at
            >= MAX_SEARCH_SECONDS
        ):
            stop_reason = "time_limit"
            break

        current_parts, depth = queue.popleft()

        try:
            with ExitStack() as stack:

                directory_fd = open_directory(
                    root["path"],
                    current_parts,
                    stack,
                )

                # 不跨越其他设备的文件系统边界。
                if (
                    os.fstat(directory_fd).st_dev
                    != root_device
                ):
                    skipped_directories += 1
                    continue

                with os.scandir(
                    directory_fd
                ) as entries:

                    for entry in entries:

                        # =====================
                        # 6.1 检查搜索资源限制
                        # =====================

                        if (
                            scanned_entries
                            >= MAX_SCANNED_ENTRIES
                        ):
                            stop_reason = "scan_limit"
                            break

                        if (
                            time.monotonic()
                            - started_at
                            >= MAX_SEARCH_SECONDS
                        ):
                            stop_reason = "time_limit"
                            break

                        scanned_entries += 1

                        # =====================
                        # 6.2 文件名安全过滤
                        # =====================

                        if not is_visible_name(
                            entry.name
                        ):
                            continue

                        try:
                            info = entry.stat(
                                follow_symlinks=False
                            )

                        except OSError:
                            continue

                        mode = info.st_mode

                        if stat.S_ISDIR(mode):
                            kind = "directory"

                        elif stat.S_ISREG(mode):
                            kind = "file"

                        else:
                            # 不返回符号链接、Socket、
                            # 设备文件等特殊条目。
                            continue

                        child_parts = (
                            current_parts + [
                                entry.name
                            ]
                        )

                        # =====================
                        # 6.3 决定是否搜索子目录
                        # =====================

                        if (
                            kind == "directory"
                            and recursive
                        ):

                            if depth < max_depth:

                                # 排除其他设备上的子挂载点。
                                if (
                                    info.st_dev
                                    == root_device
                                ):
                                    queue.append((
                                        child_parts,
                                        depth + 1,
                                    ))

                                else:
                                    skipped_directories += 1

                            else:
                                depth_limited = True

                        # =====================
                        # 6.4 文件名关键词匹配
                        # =====================

                        if (
                            keyword
                            not in entry.name.casefold()
                        ):
                            continue

                        relative_result_path = (
                            "/".join(child_parts)
                        )

                        results.append({
                            "name": entry.name,
                            "path": relative_result_path,
                            "kind": kind,
                            "size_bytes": (
                                info.st_size
                                if kind == "file"
                                else None
                            ),
                            "modified_at": (
                                datetime.fromtimestamp(
                                    info.st_mtime,
                                    timezone.utc,
                                ).isoformat()
                            ),
                        })

                        if len(results) >= limit:
                            stop_reason = "result_limit"
                            break

        except OSError as exc:

            if current_parts == base_parts:
                raise DirectoryUnavailableError(
                    "搜索起始目录不可访问"
                ) from exc

            skipped_directories += 1
            continue

        if stop_reason is not None:
            break

        if not recursive:
            break

    # =====================================
    # 7. 排序及结果汇总
    # =====================================

    results.sort(
        key=lambda item: (
            item["kind"] != "directory",
            item["name"].casefold(),
            item["path"].casefold(),
        )
    )

    incomplete = (
        stop_reason is not None
        or depth_limited
        or skipped_directories > 0
    )

    return {
        "root_id": root_id,
        "path": relative_path,
        "query": query,
        "recursive": recursive,
        "max_depth": max_depth,
        "scanned_entries": scanned_entries,
        "returned": len(results),
        "incomplete": incomplete,
        "stop_reason": stop_reason,
        "depth_limited": depth_limited,
        "skipped_directories": skipped_directories,
        "items": results,
    }
