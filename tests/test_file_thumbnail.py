"""缩略图离线回归：真实图片、缓存失效、资源限制及原有文件访问边界。"""

import io
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.api.files import router
from app.services import file_manager


class FileThumbnailTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.root = Path(temporary) / "files"
        self.root.mkdir()
        self.cache = Path(temporary) / "cache"
        roots = [{"root_id": "test", "label": "临时图片", "path": str(self.root),
                  "kind": "home", "filesystem": None, "writable": True}]
        self.enterContext(patch.object(file_manager, "list_storage_roots", return_value=roots))
        app = FastAPI()
        app.include_router(router)
        self.client = self.enterContext(TestClient(app))

    def image(self, name="photo.JPG", size=(1200, 600), color="red", **kwargs):
        path = self.root / name
        with Image.new("RGB", size, color) as image:
            image.save(path, **kwargs)
        return path

    def get(self, path="photo.JPG", **kwargs):
        return self.client.get("/api/v1/files/thumbnail", params={"root_id": "test", "path": path}, **kwargs)

    def use_cache(self):
        from app.services import file_thumbnail
        self.enterContext(patch.object(file_thumbnail, "CACHE_DIRECTORY", self.cache))
        return file_thumbnail

    def test_large_photo_returns_small_oriented_thumbnail_and_security_headers(self):
        # 原文件超过 15 MiB，像素内容本身很小，避免测试占用大量内存。
        exif = Image.Exif()
        exif[274] = 6
        path = self.image(size=(600, 1200), exif=exif)
        with path.open("ab") as stream:
            stream.truncate(16 * 1024 * 1024)
        self.use_cache()
        response = self.get()
        self.assertEqual(response.status_code, 200, response.text[:100])
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertLess(len(response.content), 30_000)
        with Image.open(io.BytesIO(response.content)) as thumbnail:
            self.assertEqual(thumbnail.size, (160, 80))
            self.assertFalse(thumbnail.getexif())
        for header in ("x-content-type-options", "content-security-policy", "referrer-policy"):
            self.assertIn(header, response.headers)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["cache-control"], "private, no-cache")

    def test_supported_case_insensitive_images_keep_aspect_and_do_not_upscale(self):
        self.use_cache()
        for name in ("a.JPEG", "a.PNG", "a.webp", "a.gif", "a.bmp"):
            with self.subTest(name=name):
                self.image(name, (40, 80))
                response = self.get(name)
                self.assertEqual(response.status_code, 200, response.text[:100])
                with Image.open(io.BytesIO(response.content)) as thumbnail:
                    self.assertEqual(thumbnail.size, (40, 80))

    def test_transparency_is_flattened_on_white_without_private_metadata(self):
        self.use_cache()
        with Image.new("RGBA", (20, 40), (255, 0, 0, 0)) as image:
            image.save(self.root / "clear.png")
        response = self.get("clear.png")
        self.assertEqual(response.status_code, 200)
        with Image.open(io.BytesIO(response.content)) as thumbnail:
            self.assertGreater(min(thumbnail.getpixel((0, 0))), 245)

    def test_disk_cache_skips_repeat_decode_and_revalidates_browser_etag(self):
        service = self.use_cache()
        self.image()
        first = self.get()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(list(self.cache.glob("*.jpg"))), 1)
        with patch.object(service, "_render_thumbnail", side_effect=AssertionError("cache must skip decoding")):
            second = self.get()
            not_modified = self.get(headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(second.content, first.content)
        self.assertEqual(not_modified.status_code, 304)
        self.assertEqual(not_modified.content, b"")
        # 同大小、同 mtime 的替换仍由 inode/ctime 识别。
        old = (self.root / "photo.JPG").stat()
        self.image("replacement.jpg", color="blue")
        os.utime(self.root / "replacement.jpg", ns=(old.st_atime_ns, old.st_mtime_ns))
        os.replace(self.root / "replacement.jpg", self.root / "photo.JPG")
        changed = self.get(headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(changed.status_code, 200)
        self.assertNotEqual(changed.headers["etag"], first.headers["etag"])
        self.assertNotEqual(changed.content, first.content)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["photo.JPG"])

    def test_in_place_edit_invalidates_cache(self):
        self.use_cache()
        self.image()
        first = self.get()
        self.image(color="blue")
        self.assertNotEqual(self.get().content, first.content)

    def test_cached_thumbnail_cannot_bypass_new_symlink_or_removed_root(self):
        self.use_cache()
        path = self.image()
        self.assertEqual(self.get().status_code, 200)
        self.image("private.jpg", color="blue")
        path.unlink()
        path.symlink_to(self.root / "private.jpg")
        self.assertEqual(self.get().status_code, 403)
        with patch.object(file_manager, "list_storage_roots", return_value=[]):
            self.assertEqual(self.get().status_code, 404)

    def test_path_traversal_symlink_directory_and_fifo_are_rejected(self):
        self.use_cache()
        self.image()
        (self.root / "folder").mkdir()
        (self.root / "linked").symlink_to(self.root, target_is_directory=True)
        os.mkfifo(self.root / "pipe.jpg")
        for path, status in (("../photo.JPG", 400), ("/etc/passwd", 400), (".hidden.jpg", 400),
                             ("folder/../photo.JPG", 400), ("folder\\photo.JPG", 400),
                             ("linked/photo.JPG", 403), ("folder", 403), ("pipe.jpg", 403),
                             ("missing.jpg", 404)):
            with self.subTest(path=path):
                self.assertEqual(self.get(path).status_code, status)

    def test_unsupported_corrupt_and_truncated_images_return_clear_errors(self):
        self.use_cache()
        (self.root / "note.md").write_text("# note")
        (self.root / "fake.jpg").write_text("<svg onload='alert(1)'/>")
        photo = self.image()
        (self.root / "broken.jpg").write_bytes(photo.read_bytes()[:200])
        for path, status in (("note.md", 415), ("fake.jpg", 422), ("broken.jpg", 422)):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status_code, status, response.text)
                self.assertTrue(response.json()["detail"])
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(list(self.cache.glob("*.jpg")), [])

    def test_size_and_pixel_limits_reject_before_full_decode(self):
        service = self.use_cache()
        path = self.image()
        with patch.object(service, "MAX_SOURCE_BYTES", path.stat().st_size - 1):
            self.assertEqual(self.get().status_code, 413)
        with patch.object(service, "MAX_IMAGE_PIXELS", 100):
            self.assertEqual(self.get().status_code, 413)

    def test_concurrent_decode_limit_returns_busy_without_affecting_listing(self):
        service = self.use_cache()
        self.image()
        with patch.object(service, "DECODE_WAIT_SECONDS", 0):
            with service._DECODE_SLOTS, service._DECODE_SLOTS:
                response = self.get()
        self.assertEqual(response.status_code, 503)
        listing = self.client.get("/api/v1/files", params={"root_id": "test"})
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["items"][0]["name"], "photo.JPG")

    def test_changing_source_during_decode_does_not_cache_stale_result(self):
        service = self.use_cache()
        path = self.image()
        render = service._render_thumbnail

        def change_after_render(*args):
            result = render(*args)
            with path.open("ab") as stream:
                stream.write(b"changed")
            return result

        with patch.object(service, "_render_thumbnail", side_effect=change_after_render):
            self.assertEqual(self.get().status_code, 409)
        self.assertEqual(list(self.cache.glob("*.jpg")), [])

    def test_cache_is_bounded_and_unavailable_cache_still_allows_small_response(self):
        service = self.use_cache()
        with patch.object(service, "MAX_CACHE_ENTRIES", 2):
            for index in range(3):
                self.image(f"{index}.jpg")
                self.assertEqual(self.get(f"{index}.jpg").status_code, 200)
        self.assertLessEqual(len(list(self.cache.glob("*.jpg"))), 2)
        self.image()
        blocked = self.root / "not-a-directory"
        blocked.write_bytes(b"block")
        with patch.object(service, "CACHE_DIRECTORY", blocked):
            self.assertEqual(self.get().status_code, 200)

    def test_original_preview_and_download_remain_exact_after_thumbnail(self):
        self.use_cache()
        path = self.image()
        original = path.read_bytes()
        self.assertEqual(self.get().status_code, 200)
        params = {"root_id": "test", "path": "photo.JPG"}
        preview = self.client.get("/api/v1/files/preview", params=params)
        download = self.client.get("/api/v1/files/download", params=params)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.content, original)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, original)
        self.assertIn("attachment", download.headers["content-disposition"])


if __name__ == "__main__":
    unittest.main()
