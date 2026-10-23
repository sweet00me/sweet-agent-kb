"""文件预览接口：真实临时文件、路径边界、内容类型和 Range 协议。"""

import os
import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect, Request

from app.api.files import router
from app.api import files as files_api
from app.services import file_manager


class FilePreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pdf = b"%PDF-1.4\n0123456789\n%%EOF\n"
        (self.root / "paper.pdf").write_bytes(self.pdf)
        (self.root / "photo.PNG").write_bytes(b"\x89PNG\r\n\x1a\n" + b"image-data")
        (self.root / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"image-data")
        (self.root / "notes.md").write_text("# 实验记录\n\n**结果**", encoding="utf-8")
        (self.root / "code.py").write_text("print('<script>safe</script>')\n", encoding="utf-8")
        (self.root / "data.json").write_text('{"answer": 42}', encoding="utf-8")
        (self.root / "plain.txt").write_text("普通文本", encoding="utf-8")
        (self.root / "empty.txt").write_bytes(b"")
        (self.root / "legacy.txt").write_bytes("中文编码".encode("gb18030"))
        (self.root / "utf16.txt").write_bytes("中文编码".encode("utf-16"))
        (self.root / "archive.zip").write_bytes(b"PK\x03\x04archive")
        (self.root / "page.html").write_text("<script>alert(1)</script>")
        (self.root / "vector.svg").write_text('<svg onload="alert(1)"></svg>')
        (self.root / "fake.pdf").write_text("<html>fake</html>")
        (self.root / "binary.txt").write_bytes(b"abc\x00binary")
        (self.root / ".secret.txt").write_text("PRIVATE")
        (self.root / "credentials.json").write_text('{"secret": true}')
        (self.root / "folder").mkdir()
        (self.root / "linked.pdf").symlink_to(self.root / "paper.pdf")
        (self.root / "linked-folder").symlink_to(self.root / "folder", target_is_directory=True)
        roots = [{"root_id": "home", "label": "测试目录", "path": str(self.root),
                  "kind": "home", "filesystem": None, "writable": True}]
        self.enterContext(patch.object(file_manager, "list_storage_roots", return_value=roots))
        app = FastAPI()
        app.include_router(router)
        self.client = self.enterContext(TestClient(app))

    def get(self, path, *, info=False, **kwargs):
        return self.client.get(
            "/api/v1/files/preview" + ("/info" if info else ""),
            params={"root_id": "home", "path": path}, **kwargs,
        )

    def test_metadata_classifies_supported_files(self):
        for filename, kind, language in (
            ("paper.pdf", "pdf", None), ("photo.PNG", "image", None),
            ("photo.jpg", "image", None), ("notes.md", "markdown", "markdown"),
            ("code.py", "text", "python"), ("data.json", "text", "json"),
            ("plain.txt", "text", "text"),
        ):
            with self.subTest(filename=filename):
                response = self.get(filename, info=True)
                self.assertEqual(response.status_code, 200, response.text)
                info = response.json()
                self.assertTrue(info["can_preview"])
                self.assertEqual(info["kind"], kind)
                self.assertEqual(info["filename"], filename)
                self.assertEqual(info["language"], language)
                self.assertEqual(info["size_bytes"], (self.root / filename).stat().st_size)

    def test_binary_previews_are_inline_with_exact_content(self):
        for filename, content_type in (("paper.pdf", "application/pdf"), ("photo.PNG", "image/png"), ("photo.jpg", "image/jpeg")):
            with self.subTest(filename=filename):
                response = self.get(filename)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.content, (self.root / filename).read_bytes())
                self.assertEqual(response.headers["content-type"], content_type)
                self.assertTrue(response.headers["content-disposition"].startswith("inline;"))
                self.assertEqual(response.headers["accept-ranges"], "bytes")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_text_is_utf8_plain_text_and_code_is_not_executed(self):
        for filename, expected in (("legacy.txt", "中文编码"), ("utf16.txt", "中文编码"), ("plain.txt", "普通文本"), ("empty.txt", ""), ("code.py", "print('<script>safe</script>')\n")):
            with self.subTest(filename=filename):
                response = self.get(filename)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.text, expected)
                self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")
                self.assertIn("sandbox", response.headers["content-security-policy"])

    def test_pdf_ranges_and_head(self):
        for value, expected_start, expected_end in (("bytes=0-4", 0, 4), ("bytes=5-", 5, len(self.pdf)-1), ("bytes=-6", len(self.pdf)-6, len(self.pdf)-1), ("bytes=0-9999", 0, len(self.pdf)-1)):
            with self.subTest(range=value):
                response = self.get("paper.pdf", headers={"Range": value})
                self.assertEqual(response.status_code, 206, response.text)
                self.assertEqual(response.content, self.pdf[expected_start:expected_end+1])
                self.assertEqual(response.headers["content-range"], f"bytes {expected_start}-{expected_end}/{len(self.pdf)}")
                self.assertEqual(int(response.headers["content-length"]), len(response.content))
        response = self.client.head("/api/v1/files/preview", params={"root_id": "home", "path": "paper.pdf"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(int(response.headers["content-length"]), len(self.pdf))

    def test_invalid_ranges_return_416(self):
        for value in ("bytes=9999-", "bytes=5-1", "bytes=-0", "bytes=-", "bytes=0-1,4-5", "items=0-1", "bytes=" + "9" * 500):
            with self.subTest(range=value):
                response = self.get("paper.pdf", headers={"Range": value})
                self.assertEqual(response.status_code, 416, response.text)
                self.assertEqual(response.headers["content-range"], f"bytes */{len(self.pdf)}")

    def test_unsupported_binary_and_active_formats_offer_download(self):
        for filename in ("archive.zip", "page.html", "vector.svg", "fake.pdf", "binary.txt"):
            with self.subTest(filename=filename):
                response = self.get(filename, info=True)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(response.json()["can_preview"])
                self.assertTrue(response.json()["reason"])
                response = self.get(filename)
                self.assertEqual(response.status_code, 415, response.text)
                response = self.client.get("/api/v1/files/download", params={"root_id": "home", "path": filename})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, (self.root / filename).read_bytes())

    def test_preview_size_limit_preserves_download(self):
        from app.services import file_preview
        with patch.object(file_preview, "MAX_TEXT_BYTES", 4):
            info = self.get("plain.txt", info=True)
            self.assertEqual(info.status_code, 200, info.text)
            self.assertFalse(info.json()["can_preview"])
            self.assertEqual(self.get("plain.txt").status_code, 413)
        self.assertEqual(self.client.get("/api/v1/files/download", params={"root_id": "home", "path": "plain.txt"}).status_code, 200)

    def test_existing_path_security_rules_apply_to_both_preview_endpoints(self):
        for info in (False, True):
            for path in ("../secret", "/etc/passwd", ".secret.txt", "credentials.json", "folder/../plain.txt", "folder\\plain.txt"):
                with self.subTest(info=info, path=path):
                    self.assertEqual(self.get(path, info=info).status_code, 400)
            for path in ("linked.pdf", "linked-folder/file.txt", "folder"):
                with self.subTest(info=info, path=path):
                    self.assertEqual(self.get(path, info=info).status_code, 403)
            self.assertEqual(self.get("missing.txt", info=info).status_code, 404)
        response = self.client.get("/api/v1/files/preview/info", params={"root_id": "missing", "path": "plain.txt"})
        self.assertEqual(response.status_code, 404)

    def test_fifo_is_rejected_without_blocking(self):
        os.mkfifo(self.root / "pipe.txt")
        self.assertEqual(self.get("pipe.txt").status_code, 403)

    def test_preview_openapi_methods_have_unique_operation_ids(self):
        operations = self.client.app.openapi()["paths"]["/api/v1/files/preview"]
        self.assertEqual(set(operations), {"get", "head"})
        self.assertNotEqual(operations["get"]["operationId"], operations["head"]["operationId"])

    def test_disconnect_before_streaming_closes_verified_descriptor(self):
        actual_open = files_api.open_preview
        opened = []

        def capture_handle(*args, **kwargs):
            handle, info = actual_open(*args, **kwargs)
            opened.append(handle)
            self.addCleanup(handle.close)
            return handle, info

        scope = {"type": "http", "method": "GET", "path": "/api/v1/files/preview",
                 "headers": [], "asgi": {"version": "3.0", "spec_version": "2.4"}}
        with patch.object(files_api, "open_preview", side_effect=capture_handle):
            response = files_api.preview_file(Request(scope), "home", "paper.pdf", None)

        async def disconnected_send(message):
            raise OSError("simulated disconnect before response start")

        async def receive():
            return {"type": "http.disconnect"}

        with self.assertRaises(ClientDisconnect):
            asyncio.run(response(scope, receive, disconnected_send))
        with self.assertRaises(OSError):
            os.fstat(opened[0].fd)

    def test_stream_uses_verified_descriptor_after_path_replacement(self):
        from app.services import file_preview
        handle, info = file_preview.open_preview("home", "paper.pdf")
        self.assertTrue(info["can_preview"])
        (self.root / "paper.pdf").unlink()
        (self.root / "paper.pdf").symlink_to(self.root / ".secret.txt")
        self.assertEqual(b"".join(file_preview.iter_preview_chunks(handle, 0, len(self.pdf))), self.pdf)
        with self.assertRaises(OSError):
            os.fstat(handle.fd)


if __name__ == "__main__":
    unittest.main()
