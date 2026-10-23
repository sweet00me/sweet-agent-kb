
"""
文件：tests/test_storage.py

用途：
    测试 Sweet Research Agent 的存储发现逻辑。

测试内容：
    1. 识别用户主目录。
    2. 识别允许位置的数据盘。
    3. 排除系统目录。
    4. 排除隐藏配置目录中的挂载点。
    5. 识别未来新增磁盘的合法挂载路径。
    6. 验证存储入口 ID 稳定。

安全说明：
    使用临时目录和模拟挂载表。
    不挂载、卸载或修改任何真实磁盘。
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app.services.storage as storage


def main():

    with TemporaryDirectory() as temp_dir:

        home = (
            Path(temp_dir)
            / "home"
            / "sweet"
        )

        home.mkdir(parents=True)

        disk = home / "Storage"
        disk.mkdir()

        hidden_disk = (
            home / ".cache" / "HiddenDisk"
        )
        hidden_disk.mkdir(parents=True)

        # 模拟 findmnt 返回的内容。
        mock_mounts = [
            {
                "target": str(disk),
                "fstype": "ext4",
                "options": "rw,relatime",
            },
            {
                "target": str(hidden_disk),
                "fstype": "ext4",
                "options": "rw",
            },
            {
                "target": "/proc",
                "fstype": "proc",
                "options": "rw",
            },
            {
                "target": "/",
                "fstype": "ext4",
                "options": "rw",
            },
        ]

        # 临时替换配置。
        with (
            patch.object(
                storage,
                "HOME_ROOT",
                home,
            ),
            patch.object(
                storage,
                "MOUNT_PARENTS",
                (
                    home,
                    Path("/mnt"),
                    Path("/media"),
                    Path("/run/media"),
                ),
            ),
            patch.object(
                storage.os.path,
                "ismount",
                side_effect=lambda path: Path(path) == disk,
            ),
        ):

            print("========== 1. 存储入口 ==========")

            roots = storage.list_storage_roots(
                mock_mounts
            )

            assert len(roots) == 2

            assert roots[0]["root_id"] == "home"
            assert roots[0]["path"] == str(home)

            assert roots[1]["path"] == str(disk)
            assert roots[1]["filesystem"] == "ext4"

            print("PASS：正确识别主目录与数据盘")

            print("\n========== 2. 安全过滤 ==========")

            paths = {
                item["path"]
                for item in roots
            }

            assert "/" not in paths
            assert "/proc" not in paths
            assert str(hidden_disk) not in paths

            print("PASS：排除系统目录与隐藏挂载点")

            print("\n========== 3. 新磁盘支持 ==========")

            assert storage._is_allowed_mount_path(
                Path("/mnt/DataDisk"),
                "ext4",
            )

            assert storage._is_allowed_mount_path(
                Path("/media/sweet/USB"),
                "exfat",
            )

            assert not storage._is_allowed_mount_path(
                Path("/boot/efi"),
                "vfat",
            )

            print("PASS：支持未来的新挂载磁盘")

            print("\n========== 4. ID 稳定性 ==========")

            second_roots = storage.list_storage_roots(
                mock_mounts
            )

            assert [
                item["root_id"]
                for item in roots
            ] == [
                item["root_id"]
                for item in second_roots
            ]

            print("PASS：存储入口 ID 稳定")

    print("\n存储发现模块测试全部通过")


if __name__ == "__main__":
    main()
