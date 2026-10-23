"""前端发布与回滚：验证静态产物、串行构建、保留哈希资源、原子切换及健康失败恢复。

仅管理事先授权创建的静态发布目录，不安装软件、不操作服务、不接触数据库。
所有 HTTP 探测只访问回环地址，禁用代理及重定向；正式后端不会因前端发布重启。
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

PROJECT = Path(__file__).resolve().parents[1]
MARKER = "sweet-research-agent-static-v1\n"
RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")
HASHED_ASSET = re.compile(r".+-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+\Z")
STATIC_SUFFIXES = {".js", ".mjs", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp",
                   ".gif", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".wasm", ".bcmap", ".pfb"}


class DeploymentError(RuntimeError):
    """发布失败；当前正式版本应保持或恢复。"""


def validate_root(root):
    root = Path(root).absolute()
    if root.resolve() != root or root == Path("/") or root.is_relative_to(PROJECT) or PROJECT.is_relative_to(root):
        raise DeploymentError("发布目录必须独立于项目、真实存在且不能是符号链接")
    if root.is_relative_to(Path("/home/sweet/Storage")):
        raise DeploymentError("禁止将 Storage 数据目录用作静态发布目录")
    for directory in (root, root / "releases", root / "assets"):
        if not directory.is_dir() or directory.is_symlink():
            raise DeploymentError("先由管理员创建独立的发布目录、releases 和 assets")
        info = directory.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise DeploymentError("发布目录必须由当前发布用户拥有，且组与其他用户不可写")
    marker = root / ".sweet-static-root"
    if marker.is_symlink() or not marker.is_file() or marker.read_text() != MARKER:
        raise DeploymentError("缺少正确的 .sweet-static-root 授权标记，拒绝写入")
    return root


@contextmanager
def release_lock(root):
    fd = os.open(root / ".deploy.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentError("已有发布或回滚进行中，请等待") from exc
        yield


class AssetReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        value = values.get("src") if tag == "script" else values.get("href") if tag == "link" else None
        if value and value.startswith("/assets/"):
            self.paths.append(value)


def inspect_build(build):
    if build.is_symlink() or not build.is_dir():
        raise DeploymentError("构建目录不存在或不安全")
    files = []
    for path in build.rglob("*"):
        relative = path.relative_to(build)
        if path.is_symlink() or any(part.startswith(".") for part in relative.parts):
            raise DeploymentError("构建产物包含符号链接或隐藏路径，拒绝发布")
        if relative.parts[0] not in {"assets", "pdfjs", "index.html", "favicon.svg", "icons.svg", "manifest.webmanifest", "apple-touch-icon.png"}:
            raise DeploymentError("构建包含未授权的顶层路径，拒绝发布")
        if path.is_dir():
            continue
        if not path.is_file():
            raise DeploymentError("构建仅允许普通静态文件")
        allowed_license = relative.parts[0] == "pdfjs" and path.name.startswith("LICENSE")
        if path.name not in {"index.html", "manifest.webmanifest"} and path.suffix.lower() not in STATIC_SUFFIXES and not allowed_license:
            raise DeploymentError("构建包含非静态文件类型，拒绝发布")
        if relative.parts[0] == "assets" and not HASHED_ASSET.fullmatch(path.name):
            raise DeploymentError("assets 文件缺少版本哈希，不能作为不可变资源发布")
        files.append(path)
    index = build / "index.html"
    if not index.is_file() or index.stat().st_size > 1024 * 1024:
        raise DeploymentError("缺少有效的 index.html")
    references = AssetReferences()
    references.feed(index.read_text(encoding="utf-8"))
    for reference in references.paths:
        relative = Path(reference.lstrip("/"))
        if ".." in relative.parts or not (build / relative).is_file():
            raise DeploymentError("index.html 引用的资源缺失")
    scripts = [path for path in references.paths if Path(path).suffix in {".js", ".mjs"}]
    if not scripts:
        raise DeploymentError("index.html 缺少生产脚本引用")
    return files, scripts[0]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def read_http(base, path):
    parsed = urlsplit(base)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise DeploymentError("健康检查只允许本机回环 HTTP 服务根地址")
    request = Request(base.rstrip("/") + path, headers={"Cache-Control": "no-cache"})
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=5) as response:
        content = response.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            raise DeploymentError("健康检查响应异常过大")
        return content


def api_health(base):
    body = json.loads(read_http(base, "/api/v1/health"))
    if not isinstance(body, dict) or body.get("status") != "ok" or body.get("service") != "sweet-research-agent":
        raise DeploymentError("API 健康检查未通过；健康不代表 Agent 当前空闲")


def frontend_health(base, release):
    _, main_asset = inspect_build(release)
    if read_http(base, "/") != (release / "index.html").read_bytes():
        raise DeploymentError("Nginx 返回的首页不是新版本")
    if read_http(base, main_asset) != (release / main_asset.lstrip("/")).read_bytes():
        raise DeploymentError("Nginx 未正确返回新版本脚本")
    api_health(base)


def pointer(root, name):
    link = root / name
    if not link.is_symlink():
        if link.exists():
            raise DeploymentError(f"{name} 不是发布软链接，拒绝覆盖")
        return None
    target = Path(os.readlink(link))
    if len(target.parts) != 2 or target.parts[0] != "releases" or not RELEASE_ID.fullmatch(target.parts[1]):
        raise DeploymentError("发布链接指向未经管理的路径")
    directory = root / target
    if not directory.is_dir() or directory.is_symlink():
        raise DeploymentError("发布链接的目标版本缺失或不安全")
    return str(target)


def set_pointer(root, name, target):
    temporary = root / (".switch-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target)
        os.replace(temporary, root / name)
    finally:
        temporary.unlink(missing_ok=True)


def switch_release(root, target, base):
    old, previous = pointer(root, "current"), pointer(root, "previous")
    if old == target:
        frontend_health(base, root / target)
        return
    # 切换前先保存旧版本，强制断电也能人工选择回滚；不会删除任何旧版本。
    if old:
        set_pointer(root, "previous", old)
    try:
        set_pointer(root, "current", target)
        frontend_health(base, root / target)
    except BaseException:
        if old:
            set_pointer(root, "current", old)
        else:
            (root / "current").unlink(missing_ok=True)
        if previous:
            set_pointer(root, "previous", previous)
        else:
            (root / "previous").unlink(missing_ok=True)
        raise


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def install_assets(root, build, files):
    for source in files:
        relative = source.relative_to(build)
        if relative.parts[0] != "assets":
            continue
        destination = root / relative
        # 在创建任何目录之前逐级拒绝链接，不向链接指向的用户目录写入。
        parent = root
        for part in relative.parts[:-1]:
            parent = parent / part
            if parent.is_symlink():
                raise DeploymentError("资源池存在符号链接，拒绝写入")
            parent.mkdir(exist_ok=True, mode=0o2750)
        if destination.is_symlink():
            raise DeploymentError("资源池存在符号链接，拒绝写入")
        if destination.exists():
            if digest(source) != digest(destination):
                raise DeploymentError("相同哈希文件名的资源内容发生变化，拒绝覆盖")
            continue
        temporary = destination.parent / (".asset-" + uuid.uuid4().hex)
        try:
            shutil.copyfile(source, temporary)
            temporary.chmod(0o640)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def publish_build(root, build, base):
    files, _ = inspect_build(build)
    # 私有静态目录由管理员设为 sweet:www-data、2750；新文件继承组，不 chmod 私人目录。
    for path in build.rglob("*"):
        path.chmod(0o2750 if path.is_dir() else 0o640)
    build.chmod(0o2750)
    install_assets(root, build, files)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    destination = root / "releases" / name
    os.replace(build, destination)
    switch_release(root, "releases/" + name, base)
    return name


def snapshot_frontend(frontend, workspace):
    """在临时副本内运行 Vite，避免配置中的 public/pdfjs 复制触发 5173 页面刷新。"""
    selected = {"src", "public", "tests", "package.json", "package-lock.json", "index.html"}
    for source in frontend.iterdir():
        if source.name not in selected and not source.name.startswith(("tsconfig", "vite.config.", "eslint.config.", ".env")):
            continue
        if source.is_symlink():
            raise DeploymentError("前端快照不接受源文件链接")
        if source.is_dir():
            for path in source.rglob("*"):
                if path.is_symlink() or path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
                    raise DeploymentError("前端快照拒绝链接和数据库文件")
            ignored = (lambda directory, names: {"pdfjs"} & set(names) if Path(directory) == frontend / "public" else set())
            shutil.copytree(source, workspace / source.name, ignore=ignored)
        else:
            shutil.copyfile(source, workspace / source.name)
    # 依赖只引用已安装版本，不执行 npm ci，也不删除开发环境的 node_modules。
    (workspace / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)


def deploy(root, frontend, base):
    for dependency in ("node", "npm"):
        if not shutil.which(dependency):
            raise DeploymentError(f"缺少依赖：{dependency}")
    if not (frontend / "package-lock.json").is_file() or not (frontend / "node_modules").is_dir():
        raise DeploymentError("缺少已安装的前端依赖；请审阅 lockfile 后单独执行 npm ci")
    api_health(base)
    build = Path(tempfile.mkdtemp(prefix=".build-", dir=root / "releases"))
    workspace = Path(tempfile.mkdtemp(prefix=".workspace-", dir=root / "releases"))
    log = root / (".deploy-" + uuid.uuid4().hex + ".log")
    try:
        snapshot_frontend(frontend, workspace)
        with log.open("x", encoding="utf-8") as output:
            log.chmod(0o600)
            commands = (["npm", "run", "lint"], ["npm", "test"],
                        ["npm", "run", "build", "--", "--outDir", str(build), "--emptyOutDir"])
            for command in commands:
                print("执行：" + " ".join(command[:3]), flush=True)
                subprocess.run(command, cwd=workspace, stdout=output, stderr=subprocess.STDOUT, check=True)
        return publish_build(root, build, base)
    except BaseException:
        print(f"发布失败；旧版本保持或已恢复。检查日志：{log}", flush=True)
        raise
    finally:
        # 仅移除本次创建的临时构建目录，正式版本与共享资源始终保留。
        if build.is_symlink():
            build.unlink()
        elif build.exists():
            shutil.rmtree(build)
        shutil.rmtree(workspace)


def interrupted(_signum, _frame):
    raise KeyboardInterrupt("发布收到终止信号")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=os.environ.get("SWEET_DEPLOY_ROOT", "/srv/sweet-research-agent"))
    parser.add_argument("--url", default=os.environ.get("SWEET_DEPLOY_URL", "http://127.0.0.1:8088"))
    parser.add_argument("--frontend", type=Path, default=PROJECT / "frontend")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--rollback", nargs="?", const="previous", metavar="RELEASE", help="回滚到 previous 或指定旧版本")
    actions.add_argument("--list", action="store_true", help="只读列出当前和历史版本")
    actions.add_argument("--check", action="store_true", help="检查静态目录与反向代理健康，不发布")
    args = parser.parse_args()
    # 普通 SIGTERM 也执行失败恢复；SIGKILL/断电只能由保留的 previous 人工恢复。
    signal.signal(signal.SIGTERM, interrupted)
    try:
        root = validate_root(args.root)
        with release_lock(root):
            if args.list:
                print(json.dumps({"current": pointer(root, "current"), "previous": pointer(root, "previous"),
                                  "releases": sorted(p.name for p in (root / "releases").iterdir() if p.is_dir() and RELEASE_ID.fullmatch(p.name))}, ensure_ascii=False))
            elif args.check:
                api_health(args.url)
                current = pointer(root, "current")
                if current:
                    frontend_health(args.url, root / current)
                print("静态目录与代理健康检查通过；不会据此判断后端空闲")
            elif args.rollback is not None:
                name = args.rollback
                if name == "previous":
                    target = pointer(root, "previous")
                else:
                    if not RELEASE_ID.fullmatch(name):
                        raise DeploymentError("回滚版本 ID 不合法")
                    target = "releases/" + name
                if not target:
                    raise DeploymentError("没有可回滚的 previous 版本")
                inspect_build(root / target)
                switch_release(root, target, args.url)
                print("已回滚至：" + target)
            else:
                print("已发布前端：" + deploy(root, args.frontend.resolve(), args.url))
    except (OSError, ValueError, DeploymentError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        parser.exit(1, f"部署未完成：{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
