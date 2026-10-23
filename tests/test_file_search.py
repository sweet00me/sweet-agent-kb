
"""
文件：tests/test_file_search.py

用途：
    验证文件名搜索、递归限制和路径安全。

测试方式：
    使用临时目录与独立 FastAPI。
    不调用 DeepSeek。
    不修改服务器真实文件。
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.services.file_search as search_module
from app.api.files import router
from app.services.file_manager import UnknownRootError


def main():

    with TemporaryDirectory() as temp:

        root = Path(temp) / "home"
        workspace = root / "Workspace"
        project = workspace / "project"

        project.mkdir(parents=True)

        (workspace / "LRFormer.md").write_text(
            "测试文件",
            encoding="utf-8",
        )

        (project / "lrformer_notes.txt").write_text(
            "测试文件",
            encoding="utf-8",
        )

        (project / "ADE20K实验.md").write_text(
            "实验笔记",
            encoding="utf-8",
        )

        # 隐藏文件与符号链接不应该出现在结果里。
        (workspace / ".LRFormer_secret.md").write_text(
            "隐藏内容",
            encoding="utf-8",
        )

        outside = Path(temp) / "LRFormer_private.md"
        outside.write_text(
            "PRIVATE_TEST_DATA",
            encoding="utf-8",
        )

        (workspace / "external").symlink_to(
            Path(temp),
            target_is_directory=True,
        )

        def mock_get_root(root_id):
            if root_id != "home":
                raise UnknownRootError(
                    "存储入口不存在"
                )

            return {
                "root_id": "home",
                "path": str(root),
            }

        test_app = FastAPI()
        test_app.include_router(router)

        with (
            patch.object(
                search_module,
                "get_storage_root",
                side_effect=mock_get_root,
            ),
            TestClient(test_app) as client,
        ):

            print("========== 1. 当前目录搜索 ==========")

            response = client.get(
                "/api/v1/files/search",
                params={
                    "root_id": "home",
                    "path": "Workspace",
                    "query": "lrformer",
                    "recursive": "false",
                },
            )

            assert response.status_code == 200

            data = response.json()

            assert data["returned"] == 1
            assert data["items"][0]["name"] == (
                "LRFormer.md"
            )

            print("PASS：非递归文件名搜索正常")

            print("\n========== 2. 递归搜索 ==========")

            response = client.get(
                "/api/v1/files/search",
                params={
                    "root_id": "home",
                    "path": "Workspace",
                    "query": "LRFORMER",
                    "recursive": "true",
                },
            )

            assert response.status_code == 200

            data = response.json()

            names = {
                item["name"]
                for item in data["items"]
            }

            assert names == {
                "LRFormer.md",
                "lrformer_notes.txt",
            }

            assert data["incomplete"] is False

            print("PASS：递归搜索正常")
            print("PASS：大小写不敏感")
            print("PASS：未访问隐藏文件与外部链接")

            print("\n========== 3. 中文关键词 ==========")

            response = client.get(
                "/api/v1/files/search",
                params={
                    "root_id": "home",
                    "path": "Workspace",
                    "query": "实验",
                },
            )

            assert response.status_code == 200

            assert (
                response.json()["items"][0]["name"]
                == "ADE20K实验.md"
            )

            print("PASS：支持中文关键词")

            print("\n========== 4. 搜索深度限制 ==========")

            response = client.get(
                "/api/v1/files/search",
                params={
                    "root_id": "home",
                    "path": "Workspace",
                    "query": "lrformer",
                    "max_depth": 0,
                },
            )

            data = response.json()

            assert data["returned"] == 1
            assert data["depth_limited"] is True
            assert data["incomplete"] is True

            print("PASS：递归深度限制生效")

            print("\n========== 5. 结果数量限制 ==========")

            response = client.get(
                "/api/v1/files/search",
                params={
                    "root_id": "home",
                    "path": "Workspace",
                    "query": "lrformer",
                    "limit": 1,
                },
            )

            data = response.json()

            assert data["returned"] == 1
            assert data["incomplete"] is True
            assert data["stop_reason"] == (
                "result_limit"
            )

            print("PASS：搜索结果数量限制生效")

            print("\n========== 6. 非法路径 ==========")

            for bad_path in [
                "../",
                "/etc",
                ".ssh",
                "Workspace/../../etc",
            ]:

                response = client.get(
                    "/api/v1/files/search",
                    params={
                        "root_id": "home",
                        "path": bad_path,
                        "query": "test",
                    },
                )

                assert response.status_code == 400

            print("PASS：拒绝非法路径")

            print("\n========== 7. 无效存储入口 ==========")

            response = client.get(
                "/api/v1/files/search",
                params={
                    "root_id": "invalid",
                    "query": "test",
                },
            )

            assert response.status_code == 404

            print("PASS：无效 root_id 被拒绝")

    print("\n文件名搜索测试全部通过")


if __name__ == "__main__":
    main()
