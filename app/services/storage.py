
"""
文件：app/services/storage.py

用途：
    为 Sweet Research Agent 发现 Linux 中可供文件管理器
    浏览的存储根目录。

主要功能：
    1. 提供 Linux 用户主目录入口。
    2. 自动发现指定位置的数据盘挂载点。
    3. 支持未来新增的本地磁盘。
    4. 排除系统虚拟文件系统和不合适的挂载位置。
    5. 为每个入口生成稳定的 root_id。

安全说明：
    本模块仅发现存储位置，不读取具体文件内容。
    不执行 mount、umount 或任何磁盘修改操作。
    root_id 不是身份认证凭证。
"""

import hashlib
import json
import os
import subprocess

from pathlib import Path


# =====================================
# 1. 存储位置配置
# =====================================

HOME_ROOT = Path("/home/sweet")

# 允许自动发现数据盘的挂载位置。
MOUNT_PARENTS = (
    HOME_ROOT,
    Path("/mnt"),
    Path("/media"),
    Path("/run/media"),
)

# 第一版只发现常见本地数据文件系统。
SUPPORTED_FILESYSTEMS = {
    "ext4",
    "xfs",
    "btrfs",
    "ntfs",
    "ntfs3",
    "exfat",
    "vfat",
    "fuseblk",
}


class StorageDiscoveryError(Exception):
    """读取 Linux 挂载信息失败。"""


# =====================================
# 2. 读取当前 Linux 挂载信息
# =====================================

def _read_mount_table() -> list[dict]:
    """
    使用 findmnt 读取当前已挂载的文件系统。

    --json：
        返回结构化 JSON。

    --list：
        返回扁平列表，避免处理嵌套挂载树。

    不调用 sudo，也不会主动挂载磁盘。
    """

    try:
        result = subprocess.run(
            [
                "findmnt",
                "--json",
                "--list",
                "--output",
                "TARGET,FSTYPE,OPTIONS",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )

        data = json.loads(result.stdout)

        filesystems = data.get("filesystems")

        if not isinstance(filesystems, list):
            raise StorageDiscoveryError(
                "findmnt 未返回有效挂载列表"
            )

        return filesystems

    except StorageDiscoveryError:
        raise

    except (
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as exc:
        raise StorageDiscoveryError(
            "无法读取 Linux 挂载信息"
        ) from exc


# =====================================
# 3. 挂载位置过滤
# =====================================

def _is_allowed_mount_path(
    target: Path,
    filesystem: str,
) -> bool:
    """
    判断指定挂载位置是否允许出现在文件管理器中。

    允许：
        /home/sweet/Storage
        /home/sweet/NewDisk
        /mnt/DataDisk
        /media/sweet/USB

    排除：
        /
        /boot/efi
        /proc
        /sys
        /dev
        /home/sweet/.cache/...

    这里仅判断路径策略，不代表磁盘已实际挂载。
    """

    if not target.is_absolute():
        return False

    if filesystem.lower() not in SUPPORTED_FILESYSTEMS:
        return False

    for parent in MOUNT_PARENTS:

        if target == parent:
            return True

        if not target.is_relative_to(parent):
            continue

        relative_parts = target.relative_to(parent).parts

        # 不自动暴露隐藏配置目录下的挂载点。
        if any(
            part.startswith(".")
            for part in relative_parts
        ):
            return False

        return True

    return False


def _is_valid_mountpoint(target: Path) -> bool:
    """
    检查挂载位置实际存在且为正常目录。

    拒绝通过符号链接伪装的挂载根目录。
    """

    try:
        if not target.is_dir():
            return False

        if target.is_symlink():
            return False

        if target.resolve(strict=True) != target:
            return False

        if not os.path.ismount(target):
            return False

        if not os.access(
            target,
            os.R_OK | os.X_OK,
        ):
            return False

        return True

    except (OSError, RuntimeError):
        return False


# =====================================
# 4. 生成稳定的存储入口 ID
# =====================================

def _make_root_id(path: Path) -> str:
    """
    根据挂载路径生成稳定标识。

    注意：
        ID 只用于前端区分存储入口。
        不能仅凭 ID 就授权文件访问。
    """

    digest = hashlib.sha256(
        str(path).encode("utf-8")
    ).hexdigest()[:16]

    return f"mount-{digest}"


# =====================================
# 5. 生成存储入口
# =====================================

def list_storage_roots(
    mount_rows: list[dict] | None = None,
) -> list[dict]:
    """
    返回可供文件管理器显示的存储入口。

    mount_rows：
        默认读取真实挂载信息。
        测试时可传入模拟数据，不访问真实挂载表。

    返回：
        按主目录、其他磁盘的顺序排列。
    """

    if mount_rows is None:
        mount_rows = _read_mount_table()

    roots = []
    seen_paths = set()

    # =====================================
    # 5.1 Linux 用户主目录
    # =====================================

    if (
        HOME_ROOT.is_dir()
        and not HOME_ROOT.is_symlink()
        and os.access(
            HOME_ROOT,
            os.R_OK | os.X_OK,
        )
    ):
        roots.append({
            "root_id": "home",
            "label": "Linux 主目录",
            "path": str(HOME_ROOT),
            "kind": "home",
            "filesystem": None,
            "writable": os.access(
                HOME_ROOT,
                os.W_OK,
            ),
        })

        seen_paths.add(str(HOME_ROOT))

    # =====================================
    # 5.2 自动发现数据磁盘
    # =====================================

    for mount in mount_rows:

        raw_target = mount.get("target")
        filesystem = str(
            mount.get("fstype") or ""
        ).lower()

        options = str(
            mount.get("options") or ""
        )

        if not isinstance(raw_target, str):
            continue

        target = Path(raw_target)

        if not _is_allowed_mount_path(
            target,
            filesystem,
        ):
            continue

        if str(target) in seen_paths:
            continue

        if not _is_valid_mountpoint(target):
            continue

        is_readonly = (
            "ro" in options.split(",")
        )

        roots.append({
            "root_id": _make_root_id(target),
            "label": target.name,
            "path": str(target),
            "kind": "mount",
            "filesystem": filesystem,
            "writable": (
                not is_readonly
                and os.access(target, os.W_OK)
            ),
        })

        seen_paths.add(str(target))

    # 主目录排在第一位，其余按挂载路径排序。
    home = [
        item for item in roots
        if item["kind"] == "home"
    ]

    mounts = sorted(
        (
            item for item in roots
            if item["kind"] == "mount"
        ),
        key=lambda item: item["path"].casefold(),
    )

    return home + mounts
