
"""
文件：tests/test_file_download.py

用途：
    验证文件下载接口。

测试内容：
    1. 下载普通文本文件。
    2. 下载中文文件名。
    3. 下载较大的二进制文件。
    4. 阻止隐藏路径和路径穿越。
    5. 阻止符号链接及目录下载。
    6. 验证文件大小限制。

不访问真实科研数据。
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.services.file_manager as manager
import app.services.file_transfer as transfer

from app.api.files import router


def main():

    with TemporaryDirectory() as temp:

        root = Path(temp) / "home"
        root.mkdir()

        docs = root / "Documents"
        docs.mkdir()

        expected_text = (
            "Sweet Research Agent 文件下载测试"
        ).encode("utf-8")

        (docs / "实验记录.md").write_bytes(
            expected_text
        )

        binary_data = bytes(range(256)) * 1024

        (docs / "binary.dat").write_bytes(
            binary_data
        )

        (root / ".secret").write_text(
            "PRIVATE",
            encoding="utf-8",
        )

        (root / "link.md").symlink_to(
            docs / "实验记录.md"
        )

        (root / "linked_directory").symlink_to(
            docs,
            target_is_directory=True,
        )

        mock_roots = [{
            "root_id": "home",
            "label": "Linux 主目录",
            "path": str(root),
            "kind": "home",
            "filesystem": None,
            "writable": True,
        }]

        app = FastAPI()
        app.include_router(router)

        with (
            patch.object(
                manager,
                "list_storage_roots",
                return_value=mock_roots,
            ),
            TestClient(app) as client,
        ):

            print("========== 1. 普通文件下载 ==========")

            response = client.get(
                "/api/v1/files/download",
                params={
                    "root_id": "home",
                    "path": "Documents/实验记录.md",
                },
            )

            assert response.status_code == 200
            assert response.content == expected_text

            assert (
                "attachment"
                in response.headers["content-disposition"]
            )

            assert (
                "filename*=UTF-8''"
                in response.headers["content-disposition"]
            )

            assert (
                response.headers["cache-control"]
                == "no-store"
            )

            print("PASS：中文文件下载正确")

            print("\n========== 2. 二进制文件 ==========")

            response = client.get(
                "/api/v1/files/download",
                params={
                    "root_id": "home",
                    "path": "Documents/binary.dat",
                },
            )

            assert response.status_code == 200
            assert response.content == binary_data

            print("PASS：较大二进制文件内容完整")

            print("\n========== 3. 非法路径 ==========")

            for bad_path in [
                "../secret",
                "/etc/passwd",
                ".secret",
                "Documents/../../etc/passwd",
            ]:

                response = client.get(
                    "/api/v1/files/download",
                    params={
                        "root_id": "home",
                        "path": bad_path,
                    },
                )

                assert response.status_code == 400, (
                    bad_path,
                    response.text,
                )

            print("PASS：非法路径已拦截")

            print("\n========== 4. 符号链接 ==========")

            for path in [
                "link.md",
                "linked_directory/实验记录.md",
            ]:

                response = client.get(
                    "/api/v1/files/download",
                    params={
                        "root_id": "home",
                        "path": path,
                    },
                )

                assert response.status_code == 403

            print("PASS：符号链接不能下载")

            print("\n========== 5. 目录及不存在的文件 ==========")

            response = client.get(
                "/api/v1/files/download",
                params={
                    "root_id": "home",
                    "path": "Documents",
                },
            )

            assert response.status_code == 403

            response = client.get(
                "/api/v1/files/download",
                params={
                    "root_id": "home",
                    "path": "Documents/missing.md",
                },
            )

            assert response.status_code == 404

            print("PASS：目录和不存在的文件正确处理")

            print("\n========== 6. 下载大小限制 ==========")

            with patch.object(
                transfer,
                "MAX_DOWNLOAD_BYTES",
                4,
            ):

                response = client.get(
                    "/api/v1/files/download",
                    params={
                        "root_id": "home",
                        "path": "Documents/binary.dat",
                    },
                )

                assert response.status_code == 413

            print("PASS：下载大小限制生效")

    print("\n文件下载测试全部通过")


if __name__ == "__main__":
    main()
