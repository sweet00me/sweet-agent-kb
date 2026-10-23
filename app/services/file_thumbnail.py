"""只读缩略图：复用下载权限及安全描述符，限制解码资源，使用私有磁盘缓存。

原图上限 32 MiB / 4000 万像素，每进程同时解码至多两张，输出最长边 160。
仅首帧、处理 EXIF 方向、清除元数据、透明区域铺白；不写知识库及原文件。
"""

import hashlib
import io
import json
import os
import re
import stat
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from app.services.file_manager import DIRECTORY_FLAGS
from app.services.file_transfer import DownloadAccessError, open_download

MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
THUMBNAIL_EDGE = 160
MAX_THUMBNAIL_BYTES = 128 * 1024
DECODE_WAIT_SECONDS = 2
MAX_CACHE_ENTRIES = 1024
MAX_CACHE_BYTES = 64 * 1024 * 1024
CACHE_TTL_SECONDS = 7 * 24 * 3600
CACHE_DIRECTORY = Path.home() / ".cache" / "sweet-agent-kb" / "thumbnails-v1"
IMAGE_FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG",
                 ".webp": "WEBP", ".gif": "GIF", ".bmp": "BMP"}
_DECODE_SLOTS = threading.BoundedSemaphore(2)
_KEY_LOCKS = tuple(threading.Lock() for _ in range(32))
_CACHE_LOCK = threading.Lock()
_CACHE_NAME = re.compile(r"[0-9a-f]{64}\.jpg\Z")


class ThumbnailError(Exception):
    """受控错误：消息不包含服务器路径或图片正文。"""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Thumbnail:
    content: bytes
    etag: str


def _version(fd: int) -> tuple[int, ...]:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def _cache_directory():
    """逐级打开私有缓存；缓存路径也禁止符号链接，避免重定向写入。"""
    with ExitStack() as stack:
        directory = CACHE_DIRECTORY.absolute()
        fd = os.open(directory.anchor, DIRECTORY_FLAGS)
        stack.callback(os.close, fd)
        for part in directory.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            fd = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            stack.callback(os.close, fd)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("缩略图缓存目录权限不安全")
        yield fd


def _cache_get(key: str) -> bytes | None:
    try:
        with _cache_directory() as directory:
            fd = os.open(key + ".jpg", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or time.time() - info.st_mtime > CACHE_TTL_SECONDS:
                    return None
                content = stream.read(MAX_THUMBNAIL_BYTES + 1)
                if len(content) <= MAX_THUMBNAIL_BYTES and content.startswith(b"\xff\xd8\xff"):
                    return content
    except OSError:
        pass  # 缓存不可用时仍可生成小图，不影响浏览。
    return None


def _prune_cache(directory: int):
    """只淘汰本服务生成的小图，限制条数、字节和保留期。"""
    entries = []
    with os.scandir(directory) as iterator:
        for entry in iterator:
            if _CACHE_NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                try:
                    info = entry.stat(follow_symlinks=False)
                    entries.append((info.st_mtime, entry.name, info.st_size))
                except FileNotFoundError:
                    continue
    entries.sort()
    total, count = sum(item[2] for item in entries), len(entries)
    for modified, name, size in entries:
        if count <= MAX_CACHE_ENTRIES and total <= MAX_CACHE_BYTES and time.time() - modified <= CACHE_TTL_SECONDS:
            break
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        count -= 1
        total -= size


def _cache_put(key: str, content: bytes):
    try:
        with _CACHE_LOCK, _cache_directory() as directory:
            temporary = uuid4().hex + ".tmp"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                os.replace(temporary, key + ".jpg", src_dir_fd=directory, dst_dir_fd=directory)
                _prune_cache(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
    except OSError:
        pass


def _render_thumbnail(fd: int, image_format: str) -> bytes:
    # 检查大小后原图仍可能增长，实际读取也必须有硬上限。
    raw = os.pread(fd, MAX_SOURCE_BYTES + 1, 0)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ThumbnailError(413, "图片超过 32 MiB 缩略图大小限制，可下载后查看")
    try:
        with Image.open(io.BytesIO(raw), formats=(image_format,)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ThumbnailError(413, "图片超过 4000 万像素解码限制，可下载后查看")
            image.draft("RGB", (THUMBNAIL_EDGE, THUMBNAIL_EDGE))
            image.thumbnail((THUMBNAIL_EDGE, THUMBNAIL_EDGE), Image.Resampling.LANCZOS)
            with ImageOps.exif_transpose(image) as oriented:
                with oriented.convert("RGBA") as rgba, Image.new("RGB", oriented.size, "white") as output:
                    output.paste(rgba, mask=rgba.getchannel("A"))
                    data = io.BytesIO()
                    output.save(data, format="JPEG", quality=82)
                    return data.getvalue()
    except Image.DecompressionBombError as exc:
        raise ThumbnailError(413, "图片超过安全解码限制，可下载后查看") from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise ThumbnailError(422, "图片格式不符、已损坏或无法解码，可下载后查看") from exc


def get_thumbnail(root_id: str, relative_path: str) -> Thumbnail:
    """缓存命中也重新授权源文件；解码始终使用已校验的描述符。"""
    handle = open_download(root_id, relative_path)
    try:
        image_format = IMAGE_FORMATS.get(PurePosixPath(handle.filename).suffix.lower())
        if image_format is None:
            raise ThumbnailError(415, "此文件类型不支持缩略图，可使用原有预览或下载")
        version = _version(handle.fd)
        if version[2] > MAX_SOURCE_BYTES:
            raise ThumbnailError(413, "图片超过 32 MiB 缩略图大小上限，可下载后查看")
        key = hashlib.sha256(json.dumps(["jpeg-160-v1", root_id, relative_path, version]).encode()).hexdigest()
        lock = _KEY_LOCKS[int(key[:8], 16) % len(_KEY_LOCKS)]
        if not lock.acquire(timeout=DECODE_WAIT_SECONDS):
            raise ThumbnailError(503, "缩略图生成繁忙，请稍后刷新")
        try:
            content = _cache_get(key)
            if content is None:
                if not _DECODE_SLOTS.acquire(timeout=DECODE_WAIT_SECONDS):
                    raise ThumbnailError(503, "缩略图生成繁忙，请稍后刷新")
                try:
                    content = _render_thumbnail(handle.fd, image_format)
                finally:
                    _DECODE_SLOTS.release()
                if _version(handle.fd) != version:
                    raise ThumbnailError(409, "图片正在修改，请刷新文件列表后重试")
                _cache_put(key, content)
            if _version(handle.fd) != version:
                raise ThumbnailError(409, "图片正在修改，请刷新文件列表后重试")
            return Thumbnail(content=content, etag='"' + key + '"')
        finally:
            lock.release()
    except OSError as exc:
        raise DownloadAccessError("图片无法安全读取") from exc
    finally:
        handle.close()
