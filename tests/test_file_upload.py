"""
文件：tests/test_file_upload.py

用途：
    验证文件上传、安全限制和冲突处理。

说明：
    使用临时目录。
    不写入真实 Storage。
    不调用 DeepSeek。
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.services.file_upload as upload_module
from app.api.files import router


def main():

    with TemporaryDirectory() as temp:

        root = Path(temp) / "Storage"
        inbox = root / "PC_Files" / "Uploads"
        inbox.mkdir(parents=True)

        mock_roots = [{
            "root_id": "mount-test",
            "label": "Storage",
            "path": str(root),
            "kind": "mount",
            "filesystem": "ext4",
            "writable": True,
        }]

        test_app = FastAPI()
        test_app.include_router(router)

        with (
            patch.object(
                upload_module,
                "STORAGE_ROOT",
                root,
            ),
            patch.object(
                upload_module,
                "list_storage_roots",
                return_value=mock_roots,
            ),
            TestClient(test_app) as client,
        ):

            print("========== 1. 正常上传 ==========")

            content = "这是一份科研笔记。".encode("utf-8")

            response = client.post(
                "/api/v1/files/upload",
                files={
                    "file": (
                        "实验记录.md",
                        content,
                        "text/markdown",
                    )
                },
            )

            assert response.status_code == 201, (
                response.text
            )

            data = response.json()

            assert data["status"] == "uploaded"
            assert data["root_id"] == "mount-test"

            assert (
                inbox / "实验记录.md"
            ).read_bytes() == content

            print("PASS：中文文件上传成功")

            print("\n========== 2. 同名文件 ==========")

            response = client.post(
                "/api/v1/files/upload",
                files={
                    "file": (
                        "实验记录.md",
                        b"NEW CONTENT",
                        "text/markdown",
                    )
                },
            )

            assert response.status_code == 409

            assert (
                inbox / "实验记录.md"
            ).read_bytes() == content

            print("PASS：同名文件不会被覆盖")

            print("\n========== 3. 文件类型 ==========")

            response = client.post(
                "/api/v1/files/upload",
                files={
                    "file": (
                        "danger.sh",
                        b"echo test",
                        "text/plain",
                    )
                },
            )

            assert response.status_code == 400

            print("PASS：禁止不支持的文件类型")

            print("\n========== 4. 上传大小 ==========")

            with patch.object(
                upload_module,
                "MAX_UPLOAD_BYTES",
                4,
            ):

                response = client.post(
                    "/api/v1/files/upload",
                    files={
                        "file": (
                            "large.pdf",
                            b"1234567890",
                            "application/pdf",
                        )
                    },
                )

            assert response.status_code == 413
            assert not (
                inbox / "large.pdf"
            ).exists()

            print("PASS：超限文件被拒绝")

            print("\n========== 5. 临时文件清理 ==========")

            leftovers = [
                p.name
                for p in inbox.iterdir()
                if p.name.startswith(".incoming-")
            ]

            assert leftovers == []

            print("PASS：失败上传未遗留临时文件")

    print("\n文件上传测试全部通过")


if __name__ == "__main__":
    main()
