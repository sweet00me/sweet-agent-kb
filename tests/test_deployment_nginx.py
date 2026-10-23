"""真实临时 Nginx → FastAPI 回归：SSE 实时传输、审批方法、静态页面及文件接口。

全部监听临时回环端口，文件、上传、缓存均在临时目录；SSE 为模拟任务，不调用模型。
通过 SWEET_TEST_NGINX / SWEET_TEST_NGINX_MIME 指定未安装到系统的验证二进制及 MIME 表。
"""

import io
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from PIL import Image

from app.api import files as files_api
from app.api.streaming import StreamJob, iter_sse_events
from app.services import file_manager, file_thumbnail, file_upload
from deploy import frontend_release as release
from tests.test_deployment import make_build, make_root


@unittest.skipUnless(os.environ.get("SWEET_TEST_NGINX") or shutil.which("nginx"), "需提供 Nginx 验证二进制")
class NginxDeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.temp = Path(cls.stack.enter_context(TemporaryDirectory()))
        cls.web = make_root(cls.temp / "web")
        cls.files = cls.temp / "files"
        (cls.files / "PC_Files/Uploads").mkdir(parents=True)
        (cls.files / "notes.md").write_text("# 模拟科研笔记\n")
        (cls.files / "broken.jpg").write_bytes(b"bad image")
        with Image.new("RGB", (1200, 600), "steelblue") as image:
            image.save(cls.files / "photo.JPG")
        roots = [{"root_id": "fixture", "label": "临时文件", "path": str(cls.files),
                  "kind": "mount", "filesystem": "ext4", "writable": True}]
        for obj, key, value in ((files_api, "list_storage_roots", roots),
                                (file_manager, "list_storage_roots", roots),
                                (file_upload, "list_storage_roots", roots)):
            cls.stack.enter_context(patch.object(obj, key, return_value=value))
        cls.stack.enter_context(patch.object(file_upload, "STORAGE_ROOT", cls.files))
        cls.stack.enter_context(patch.object(file_thumbnail, "CACHE_DIRECTORY", cls.temp / "cache"))
        cls.app = FastAPI()
        cls.app.include_router(files_api.router)
        cls.streams = []
        cls.calls = []

        @cls.app.get("/api/v1/health")
        def health():
            return {"status": "ok", "service": "sweet-research-agent"}

        @cls.app.post("/api/v1/chat/stream")
        @cls.app.post("/api/v1/threads/fixture/review/stream")
        async def stream(request: Request):
            payload = await request.json()
            cls.calls.append((request.url.path, payload))
            job = StreamJob()
            gate = threading.Event()
            cls.streams.append(gate)
            job.emit("started", {"status": "running"})

            def finish():
                gate.wait(4)
                job.emit("completed", {"status": "completed", "answer": "模拟结果"})
                job.emit("__end__", {})

            threading.Thread(target=finish, daemon=True).start()
            return StreamingResponse(iter_sse_events(job), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        backend_socket = socket.socket()
        backend_socket.bind(("127.0.0.1", 0))
        backend_port = backend_socket.getsockname()[1]
        cls.backend = uvicorn.Server(uvicorn.Config(cls.app, log_level="error"))
        backend_thread = threading.Thread(target=lambda: cls.backend.run(sockets=[backend_socket]), daemon=True)
        backend_thread.start()

        def stop_backend():
            for gate in cls.streams:
                gate.set()
            cls.backend.should_exit = True
            backend_thread.join(timeout=8)
            backend_socket.close()

        cls.stack.callback(stop_backend)
        deadline = time.monotonic() + 5
        while not cls.backend.started and time.monotonic() < deadline:
            time.sleep(.01)
        if not cls.backend.started:
            raise RuntimeError("隔离 FastAPI 启动失败")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        cls.base = f"http://127.0.0.1:{port}"
        binary = os.environ.get("SWEET_TEST_NGINX") or shutil.which("nginx")
        mime = os.environ.get("SWEET_TEST_NGINX_MIME", "/etc/nginx/mime.types")
        site = (release.PROJECT / "deploy/nginx/sweet-research-agent.conf").read_text()
        site = site.replace("127.0.0.1:8088", f"127.0.0.1:{port}").replace("localhost:8088", f"localhost:{port}")
        site = site.replace("127.0.0.1:8010", f"127.0.0.1:{backend_port}")
        site = site.replace("/srv/sweet-research-agent", str(cls.web)).replace("/etc/nginx/mime.types", mime)
        temporary_paths = "\n".join(f'{name}_temp_path "{cls.temp}/{name}";' for name in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"))
        config = cls.temp / "nginx.conf"
        config.write_text(f'pid "{cls.temp}/nginx.pid";\nerror_log "{cls.temp}/nginx.error.log" warn;\nworker_processes 1;\nevents {{ worker_connections 128; }}\nhttp {{\n{temporary_paths}\n{site}\n}}\n')
        syntax = subprocess.run([binary, "-p", str(cls.temp) + "/", "-c", str(config), "-t"], capture_output=True, text=True)
        if syntax.returncode:
            raise RuntimeError(syntax.stderr)
        cls.nginx_log = cls.stack.enter_context((cls.temp / "nginx-process.log").open("w"))
        cls.nginx = subprocess.Popen([binary, "-p", str(cls.temp) + "/", "-c", str(config), "-g", "daemon off;"], stdout=cls.nginx_log, stderr=subprocess.STDOUT)

        def stop_nginx():
            if cls.nginx.poll() is None:
                cls.nginx.send_signal(signal.SIGQUIT)
                try:
                    cls.nginx.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    cls.nginx.kill()  # 仅清理本测试新建的隔离子进程，绝不查找或停止生产进程。
                    cls.nginx.wait(timeout=2)

        cls.stack.callback(stop_nginx)
        deadline = time.monotonic() + 5
        while True:
            try:
                release.api_health(cls.base)
                break
            except OSError:
                if time.monotonic() >= deadline or cls.nginx.poll() is not None:
                    raise RuntimeError("隔离 Nginx 启动失败")
                time.sleep(.01)
        cls.client = cls.stack.enter_context(httpx.Client(base_url=cls.base, trust_env=False, timeout=3))
        build = make_build(cls.web / "releases/.initial")
        (build / "assets/worker-abcdefgh.mjs").write_text("export const fixture=1;")
        (build / "pdfjs/wasm").mkdir(parents=True)
        (build / "pdfjs/wasm/fixture.wasm").write_bytes(b"\0asm\x01\0\0\0")
        cls.initial = release.publish_build(cls.web, build, cls.base)

    def setUp(self):
        release.set_pointer(self.web, "current", "releases/" + self.initial)

    def test_frontend_spa_cache_mime_and_missing_assets(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(self.client.get("/some/client/route").content, response.content)
        resource = self.client.get("/assets/worker-abcdefgh.mjs")
        self.assertEqual(resource.headers["content-type"], "application/javascript")
        self.assertIn("immutable", resource.headers["cache-control"])
        self.assertEqual(self.client.get("/pdfjs/wasm/fixture.wasm").headers["content-type"], "application/wasm")
        self.assertEqual(self.client.get("/assets/missing.js").status_code, 404)

    def test_api_proxy_and_cross_site_origin(self):
        self.assertEqual(self.client.get("/api/v1/health").json()["service"], "sweet-research-agent")
        self.assertEqual(self.client.get("/api/v1/files/roots").json()["count"], 1)
        response = self.client.get("/api/v1/health", headers={"Origin": "https://untrusted.invalid"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/api/v1/health", headers={"Origin": "https://sweetserver.taile94715.ts.net"}).status_code, 200)

    def test_sse_started_arrives_before_task_finishes_and_post_is_not_replayed(self):
        for endpoint, payload in (("/api/v1/chat/stream", {"message": "模拟请求"}),
                                  ("/api/v1/threads/fixture/review/stream", {"decisions": {"tool": "approve"}, "expected_fingerprints": {"tool": "fixture"}})):
            with self.subTest(endpoint=endpoint):
                previous = len(self.calls)
                with self.client.stream("POST", endpoint, json=payload) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
                    lines = response.iter_lines()
                    self.assertEqual(next(lines), "event: started")
                    self.assertFalse(self.streams[-1].is_set(), "首个 SSE 事件不能等任务结束才到达")
                    self.streams[-1].set()
                    self.assertIn("event: completed", list(lines))
                self.assertEqual(self.calls[previous:], [(endpoint, payload)])

    def test_files_browse_search_upload_download_preview_and_thumbnail(self):
        params = {"root_id": "fixture", "path": "photo.JPG"}
        self.assertEqual(self.client.get("/api/v1/files", params={"root_id": "fixture"}).status_code, 200)
        found = self.client.get("/api/v1/files/search", params={"root_id": "fixture", "query": "photo"})
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()["items"][0]["name"], "photo.JPG")
        original = (self.files / "photo.JPG").read_bytes()
        for endpoint in ("download", "preview"):
            response = self.client.get("/api/v1/files/" + endpoint, params=params)
            self.assertEqual(response.content, original)
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertIn("attachment" if endpoint == "download" else "inline", response.headers["content-disposition"])
        partial = self.client.get("/api/v1/files/preview", params=params, headers={"Range": "bytes=0-15"})
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, original[:16])
        thumbnail = self.client.get("/api/v1/files/thumbnail", params=params)
        self.assertEqual(thumbnail.headers["content-type"], "image/jpeg")
        with Image.open(io.BytesIO(thumbnail.content)) as image:
            self.assertEqual(image.size, (160, 80))
        self.assertEqual(self.client.get("/api/v1/files/thumbnail", params=params, headers={"If-None-Match": thumbnail.headers["etag"]}).status_code, 304)
        self.assertEqual(self.client.get("/api/v1/files/thumbnail", params={**params, "path": "broken.jpg"}).status_code, 422)
        upload = self.client.post("/api/v1/files/upload", files={"file": ("uploaded.md", b"# fixture upload", "text/markdown")})
        self.assertEqual(upload.status_code, 201, upload.text)
        self.assertEqual((self.files / "PC_Files/Uploads/uploaded.md").read_bytes(), b"# fixture upload")

    def test_private_paths_and_sources_are_never_static_resources(self):
        for path in ("/.env", "/.git/config", "/app/api/main.py", "/data.sqlite3", "/assets/source.map"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_nginx_rejects_upload_over_52_mib_before_reading_body(self):
        # 只发送请求头，避免为了边界测试传输大文件或写入数据。
        from urllib.parse import urlsplit
        with socket.create_connection(("127.0.0.1", urlsplit(self.base).port), timeout=3) as connection:
            connection.sendall(("POST /api/v1/files/upload HTTP/1.1\r\nHost: localhost\r\n"
                                f"Content-Length: {52 * 1024 * 1024 + 1}\r\nConnection: close\r\n\r\n").encode())
            self.assertTrue(connection.recv(128).startswith(b"HTTP/1.1 413"))

    def test_zz_real_frontend_script_builds_publishes_and_rolls_back_without_backend_restart(self):
        public_files = release.PROJECT / "frontend/public/pdfjs"
        before = {str(path): path.stat().st_mtime_ns for path in public_files.rglob("*") if path.is_file()}
        environment = {**os.environ, "SWEET_DEPLOY_PYTHON": os.sys.executable}
        command = ["bash", str(release.PROJECT / "scripts/deploy.sh"), "--root", str(self.web), "--url", self.base]
        process = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=120)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertIn('<div id="root">', self.client.get("/").text)
        current = release.pointer(self.web, "current")
        self.assertNotEqual(current, "releases/" + self.initial)
        if os.environ.get("SWEET_TEST_PLAYWRIGHT"):
            browser_check = r'''
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'
const { chromium } = await import(pathToFileURL(process.env.SWEET_TEST_PLAYWRIGHT).href)
const browser = await chromium.launch({ headless: true })
try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })
  const errors = []; page.on('pageerror', error => errors.push(error.message))
  await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort())
  await page.goto(process.env.SWEET_TEST_FRONTEND_URL)
  await page.getByRole('button', { name: '知识库', exact: true }).filter({ visible: true }).click()
  await page.getByRole('button', { name: /文档管理.*浏览服务器目录/ }).click()
  const name = page.getByRole('button', { name: 'photo.JPG', exact: true })
  await name.scrollIntoViewIfNeeded()
  const row = name.locator('..').locator('..')
  await row.locator('img').waitFor()
  await row.locator('svg.lucide-file-text').waitFor({ state: 'detached' })
  assert.deepEqual(await row.locator('img').evaluate(img => ({ width: img.width, height: img.height, fit: getComputedStyle(img).objectFit, natural: img.naturalWidth })),
    { width: 56, height: 56, fit: 'cover', natural: 160 })
  await name.click()
  await page.getByRole('dialog', { name: '文件预览' }).locator('img').waitFor()
  await page.getByRole('button', { name: '关闭预览', exact: true }).click()
  const downloaded = page.waitForEvent('download')
  await page.getByRole('link', { name: '下载 photo.JPG', exact: true }).click()
  assert.equal((await downloaded).suggestedFilename(), 'photo.JPG')
  assert.deepEqual(errors, [])
} finally { await browser.close() }
'''
            browser = subprocess.run(["node", "--input-type=module", "-e", browser_check],
                                     env={**environment, "SWEET_TEST_FRONTEND_URL": self.base},
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(browser.returncode, 0, browser.stdout + browser.stderr)
        rollback = subprocess.run([*command, "--rollback"], capture_output=True, text=True, env=environment, timeout=10)
        self.assertEqual(rollback.returncode, 0, rollback.stdout + rollback.stderr)
        self.assertEqual(release.pointer(self.web, "current"), "releases/" + self.initial)
        self.assertTrue(self.backend.started)
        self.assertIsNone(self.nginx.poll())
        self.assertEqual({str(path): path.stat().st_mtime_ns for path in public_files.rglob("*") if path.is_file()}, before,
                         "正式发布不能重写开发服务器监听的 PDF.js 资源")


if __name__ == "__main__":
    unittest.main()
