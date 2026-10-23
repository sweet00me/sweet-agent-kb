
"""
文件：tests/test_file_browser.py

用途：
    验证文件夹浏览和路径访问控制。

测试方式：
    使用临时目录作为文件系统。
    不读取服务器真实文件。
    不调用 DeepSeek 或 LangGraph。
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.services.file_manager as manager
from app.api.files import router


def main():

    with TemporaryDirectory() as temp:

        root = Path(temp) / "home"
        workspace = root / "Workspace"
        storage = root / "Storage"

        workspace.mkdir(parents=True)
        storage.mkdir()

        (root / ".ssh").mkdir()

        (workspace / "data").mkdir()

        (workspace / "alpha.md").write_text(
            "# 第一份科研笔记",
            encoding="utf-8",
        )

        (workspace / "beta.md").write_text(
            "# 第二份科研笔记",
            encoding="utf-8",
        )

        (root / ".env").write_text(
            "SECRET=testing",
            encoding="utf-8",
        )

        (root / "outside").symlink_to(
            Path(temp),
            target_is_directory=True,
        )

        mock_roots = [
            {
                "root_id": "home",
                "label": "Linux 主目录",
                "path": str(root),
                "kind": "home",
                "filesystem": None,
                "writable": True,
            }
        ]

        test_app = FastAPI()
        test_app.include_router(router)

        with (
            patch.object(
                manager,
                "list_storage_roots",
                return_value=mock_roots,
            ),
            TestClient(test_app) as client,
        ):

            print("========== 1. 根目录浏览 ==========")

            response = client.get(
                "/api/v1/files",
                params={"root_id": "home"},
            )

            assert response.status_code == 200

            data = response.json()

            names = {
                item["name"]
                for item in data["items"]
            }

            assert "Workspace" in names
            assert "Storage" in names
            assert ".ssh" not in names
            assert ".env" not in names
            assert "outside" not in names

            print("PASS：目录浏览成功")
            print("PASS：隐藏文件和符号链接已过滤")

            print("\n========== 2. 子目录浏览 ==========")

            response = client.get(
                "/api/v1/files",
                params={
                    "root_id": "home",
                    "path": "Workspace",
                },
            )

            assert response.status_code == 200

            data = response.json()

            assert data["parent_path"] == ""

            assert data["total"] == 3

            assert data["items"][0]["name"] == "data"
            assert data["items"][0]["kind"] == "directory"

            print("PASS：能够进入子目录")
            print("PASS：目录优先排序")

            print("\n========== 3. 分页测试 ==========")

            first = client.get(
                "/api/v1/files",
                params={
                    "root_id": "home",
                    "path": "Workspace",
                    "limit": 1,
                    "offset": 0,
                },
            ).json()

            second = client.get(
                "/api/v1/files",
                params={
                    "root_id": "home",
                    "path": "Workspace",
                    "limit": 1,
                    "offset": 1,
                },
            ).json()

            assert first["has_more"] is True

            assert (
                first["items"][0]["name"]
                != second["items"][0]["name"]
            )

            print("PASS：分页正常")

            print("\n========== 4. 非法路径 ==========")

            for bad_path in [
                "../",
                "/etc",
                "Workspace/../../etc",
                ".ssh",
                "Workspace/.git",
                "Workspace//data",
            ]:

                response = client.get(
                    "/api/v1/files",
                    params={
                        "root_id": "home",
                        "path": bad_path,
                    },
                )

                assert response.status_code == 400, (
                    bad_path,
                    response.text,
                )

            print("PASS：路径穿越和隐藏目录被拒绝")

            print("\n========== 5. 符号链接 ==========")

            response = client.get(
                "/api/v1/files",
                params={
                    "root_id": "home",
                    "path": "outside",
                },
            )

            assert response.status_code == 403

            print("PASS：不能进入目录符号链接")

            print("\n========== 6. 无效存储入口 ==========")

            response = client.get(
                "/api/v1/files",
                params={
                    "root_id": "mount-invalid"
                },
            )

            assert response.status_code == 404

            print("PASS：无效 root_id 被拒绝")

    print("\n安全文件夹浏览测试全部通过")


if __name__ == "__main__":
    main()
