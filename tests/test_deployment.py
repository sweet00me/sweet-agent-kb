"""部署离线回归：临时静态目录、构建失败、原子切换、旧资源、回滚及挂载保护。

不运行生产 Agent、不连接真实模型，不修改系统服务、Serve 或正式数据库。
"""

import functools
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from deploy import check_runtime as runtime
from deploy import frontend_release as release


def make_root(path):
    for directory in (path, path / "releases", path / "assets"):
        directory.mkdir(exist_ok=True)
        directory.chmod(0o2750)
    (path / ".sweet-static-root").write_text(release.MARKER)
    return path


def make_build(path, letter="a"):
    (path / "assets").mkdir(parents=True)
    (path / "assets" / ("index-" + letter * 8 + ".js")).write_text(f'window.fixture="{letter}";')
    (path / "index.html").write_text(f'<div id="root"></div><script type="module" src="/assets/index-{letter * 8}.js"></script>')
    return path


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(TemporaryDirectory()))
        self.root = make_root(self.temp / "web")
        self.bad_index = False
        self.health_ok = True
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/api/v1/health":
                    body = json.dumps({"status": "ok" if owner.health_ok else "bad", "service": "sweet-research-agent"}).encode()
                else:
                    path = owner.root / "assets" / self.path.removeprefix("/assets/") if self.path.startswith("/assets/") else owner.root / "current/index.html"
                    body = path.read_bytes() if path.exists() else b"missing"
                    if owner.bad_index and b"bbbbbbbb" in body:
                        body = b"incorrect frontend"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        self.base = f"http://127.0.0.1:{server.server_port}"

    def publish(self, letter):
        return release.publish_build(self.root, make_build(self.root / "releases" / (".test-" + letter), letter), self.base)

    def test_atomic_publish_preserves_old_assets_and_rollback(self):
        a = self.publish("a")
        b = self.publish("b")
        self.assertEqual(release.pointer(self.root, "current"), "releases/" + b)
        self.assertEqual(release.pointer(self.root, "previous"), "releases/" + a)
        self.assertIn(b'fixture="a"', release.read_http(self.base, "/assets/index-aaaaaaaa.js"))
        self.assertEqual((self.root / "releases" / b / "index.html").stat().st_mode & 0o777, 0o640)
        self.assertEqual((self.root / "assets/index-aaaaaaaa.js").stat().st_mode & 0o777, 0o640)
        release.switch_release(self.root, "releases/" + a, self.base)
        self.assertEqual(release.pointer(self.root, "current"), "releases/" + a)
        self.assertEqual(release.pointer(self.root, "previous"), "releases/" + b)

    def test_failed_health_restores_both_pointers_and_keeps_old_version(self):
        a = self.publish("a")
        self.bad_index = True
        with self.assertRaises(release.DeploymentError):
            self.publish("b")
        self.assertEqual(release.pointer(self.root, "current"), "releases/" + a)
        self.assertIsNone(release.pointer(self.root, "previous"))
        self.assertIn(b"aaaaaaaa", release.read_http(self.base, "/"))

    def test_first_release_failure_leaves_no_current_link(self):
        self.bad_index = True
        with self.assertRaises(release.DeploymentError):
            self.publish("b")
        self.assertFalse((self.root / "current").exists())
        self.assertFalse((self.root / "current").is_symlink())

    def test_existing_immutable_asset_cannot_be_replaced(self):
        a = self.publish("a")
        build = make_build(self.root / "releases/.collision")
        (build / "assets/index-aaaaaaaa.js").write_text("different bytes")
        with self.assertRaises(release.DeploymentError):
            release.publish_build(self.root, build, self.base)
        self.assertEqual(release.pointer(self.root, "current"), "releases/" + a)
        self.assertIn(b'fixture="a"', release.read_http(self.base, "/assets/index-aaaaaaaa.js"))

    def test_secrets_source_database_and_symlinks_are_not_publishable(self):
        secret = self.temp / "private.txt"
        secret.write_text("fixture secret")
        for index, name in enumerate((".env", "source.py", "data.sqlite3", "assets/file.map", "assets/linked-abcdefgh.js")):
            build = make_build(self.root / "releases" / (".invalid-" + str(index)))
            path = build / name
            path.parent.mkdir(exist_ok=True)
            if "linked" in name:
                path.symlink_to(secret)
            else:
                path.write_text("forbidden")
            with self.subTest(name=name), self.assertRaises(release.DeploymentError):
                release.inspect_build(build)
        self.assertEqual(secret.read_text(), "fixture secret")

    def test_resource_pool_directory_symlink_cannot_create_external_directories(self):
        outside = self.temp / "outside"
        outside.mkdir()
        (self.root / "assets/sub").symlink_to(outside, target_is_directory=True)
        build = make_build(self.root / "releases/.linked")
        nested = build / "assets/sub/new"
        nested.mkdir(parents=True)
        (nested / "file-abcdefgh.js").write_text("safe fixture")
        with self.assertRaises(release.DeploymentError):
            release.publish_build(self.root, build, self.base)
        self.assertEqual(list(outside.iterdir()), [])

    def test_unmarked_writable_or_linked_root_is_rejected(self):
        self.assertEqual(release.validate_root(self.root), self.root)
        (self.root / ".sweet-static-root").unlink()
        with self.assertRaises(release.DeploymentError):
            release.validate_root(self.root)
        (self.root / ".sweet-static-root").write_text(release.MARKER)
        self.root.chmod(0o2770)
        with self.assertRaises(release.DeploymentError):
            release.validate_root(self.root)
        link = self.temp / "linked"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(release.DeploymentError):
            release.validate_root(link)

    def test_concurrent_release_is_refused(self):
        with release.release_lock(self.root), self.assertRaises(release.DeploymentError):
            with release.release_lock(self.root):
                self.fail("second release acquired the lock")

    def test_health_cannot_use_external_services_or_wrong_service(self):
        for url in ("https://example.com", "http://example.com", "http://user:password@127.0.0.1"):
            with self.subTest(url=url), self.assertRaises(release.DeploymentError):
                release.api_health(url)
        self.health_ok = False
        with self.assertRaises(release.DeploymentError):
            release.api_health(self.base)

    def fake_frontend(self):
        frontend = self.temp / "frontend"
        frontend.mkdir()
        (frontend / "node_modules").mkdir()
        (frontend / "package-lock.json").write_text("{}")
        (frontend / "package.json").write_text("{}")
        binaries = self.temp / "bin"
        binaries.mkdir()
        npm = binaries / "npm"
        npm.write_text("#!/usr/bin/python3\n" +
                       "import os,sys,pathlib\n" +
                       "with open(os.environ['FIXTURE_COMMANDS'],'a') as log: log.write(' '.join(sys.argv[1:])+'\\n')\n" +
                       "if os.environ.get('FIXTURE_FAIL') in sys.argv[1:]: sys.exit(7)\n" +
                       "if 'build' in sys.argv:\n" +
                       " p=pathlib.Path(sys.argv[sys.argv.index('--outDir')+1]); (p/'assets').mkdir()\n" +
                       " (p/'assets/index-bbbbbbbb.js').write_text('window.fixture=2;')\n" +
                       " (p/'index.html').write_text('<script src=\"/assets/index-bbbbbbbb.js\"></script>')\n")
        npm.chmod(0o755)
        return frontend, {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                          "FIXTURE_COMMANDS": str(self.temp / "commands.log")}

    def run_script(self, frontend, environment, *args):
        return subprocess.run(["bash", str(release.PROJECT / "scripts/deploy.sh"), "--root", str(self.root),
                               "--url", self.base, "--frontend", str(frontend), *args],
                              env=environment, text=True, capture_output=True, timeout=10)

    def test_script_build_failure_never_switches_or_restarts_services(self):
        a = self.publish("a")
        frontend, environment = self.fake_frontend()
        environment["FIXTURE_FAIL"] = "build"
        result = self.run_script(frontend, environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(release.pointer(self.root, "current"), "releases/" + a)
        commands = (self.temp / "commands.log").read_text().splitlines()
        self.assertEqual(commands[:2], ["run lint", "test"])
        self.assertTrue(commands[2].startswith("run build -- --outDir "))
        self.assertFalse(any(path.name.startswith(".build-") for path in (self.root / "releases").iterdir()))

    def test_script_lint_failure_never_builds(self):
        self.publish("a")
        frontend, environment = self.fake_frontend()
        environment["FIXTURE_FAIL"] = "lint"
        self.assertNotEqual(self.run_script(frontend, environment).returncode, 0)
        self.assertEqual((self.temp / "commands.log").read_text().splitlines(), ["run lint"])

    def test_script_full_publish_list_and_rollback(self):
        a = self.publish("a")
        frontend, environment = self.fake_frontend()
        result = self.run_script(frontend, environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        b = release.pointer(self.root, "current")
        self.assertNotEqual(b, "releases/" + a)
        listing = self.run_script(frontend, environment, "--list")
        self.assertEqual(json.loads(listing.stdout)["previous"], "releases/" + a)
        result = self.run_script(frontend, environment, "--rollback")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(release.pointer(self.root, "current"), "releases/" + a)
        self.assertEqual(release.pointer(self.root, "previous"), b)
        self.assertEqual(len((self.temp / "commands.log").read_text().splitlines()), 3)


class RuntimeGuardTests(unittest.TestCase):
    def test_wrong_uuid_readonly_and_non_mount_are_rejected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for mounted, uuid, options in ((False, "expected", "rw"), (True, "wrong", "rw"), (True, "expected", "ro")):
                result = subprocess.CompletedProcess([], 0, json.dumps({"filesystems": [{"target": str(root), "uuid": uuid, "fstype": "ext4", "options": options}]}))
                with patch.object(runtime.os.path, "ismount", return_value=mounted), patch.object(runtime.subprocess, "run", return_value=result), self.assertRaises(RuntimeError):
                    runtime.check_mount(root, "expected")

    def test_expected_independent_rw_partition_passes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = subprocess.CompletedProcess([], 0, json.dumps({"filesystems": [{"target": str(root), "uuid": "expected", "fstype": "ext4", "options": "rw,relatime"}]}))
            with patch.object(runtime.os.path, "ismount", return_value=True), patch.object(runtime.subprocess, "run", return_value=result), redirect_stdout(io.StringIO()):
                runtime.check_mount(root, "expected")

    def test_occupied_port_is_refused_without_stopping_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with self.assertRaises(RuntimeError):
                runtime.check_port(port=port)
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass

    def test_quiescence_blocks_unconfirmed_execution_but_preserves_pending_approval(self):
        with TemporaryDirectory() as temporary:
            database = Path(temporary) / "sessions.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE api_requests(status TEXT)")
                connection.execute("INSERT INTO api_requests VALUES ('pending_approval')")
            with redirect_stdout(io.StringIO()):
                runtime.check_quiescence(database)
            for status in ("accepted", "running", "uncertain"):
                with sqlite3.connect(database) as connection:
                    connection.execute("DELETE FROM api_requests")
                    connection.execute("INSERT INTO api_requests VALUES (?)", (status,))
                before = database.read_bytes()
                with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                    runtime.check_quiescence(database)
                self.assertEqual(database.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
