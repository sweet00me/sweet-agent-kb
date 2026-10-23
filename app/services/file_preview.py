"""只读文件预览；复用下载服务的目录描述符和路径安全校验。"""

import os
import re
from pathlib import PurePosixPath

from app.services.file_transfer import (
    CHUNK_SIZE,
    DownloadAccessError,
    DownloadHandle,
    open_download,
)

MAX_TEXT_BYTES = 1024 * 1024
MAX_PDF_BYTES = 64 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024

TEXT_LANGUAGES = {
    ".txt": "text", ".log": "text", ".csv": "text",
    ".py": "python", ".json": "json", ".jsonl": "json",
    ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".css": "css", ".scss": "scss",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".ini": "ini", ".cfg": "ini", ".conf": "text",
    ".sh": "bash", ".bash": "bash", ".sql": "sql",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
    ".java": "java", ".go": "go", ".rs": "rust", ".xml": "xml",
}


class TextPreviewError(ValueError):
    """内容无法按受限文本安全预览。"""


def read_preview_text(handle: DownloadHandle) -> str:
    """限制实际读取字节数，兼容 UTF-8、带 BOM 的 UTF-16 和 GB18030。"""
    raw = os.pread(handle.fd, MAX_TEXT_BYTES + 1, 0)
    if len(raw) > MAX_TEXT_BYTES:
        raise TextPreviewError("文本超过预览大小限制，可下载后查看")
    try:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = raw.decode("utf-16")
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("gb18030")
    except UnicodeDecodeError as exc:
        raise TextPreviewError("无法识别文本编码，可下载后查看") from exc
    if any(ord(char) < 32 and char not in "\t\n\r\f" for char in text):
        raise TextPreviewError("文件包含二进制内容，可下载后查看")
    return text


def open_preview(root_id: str, relative_path: str) -> tuple[DownloadHandle, dict]:
    """安全打开文件并判断预览能力；不重新通过用户路径读取文件。"""
    handle = open_download(root_id, relative_path)
    try:
        extension = PurePosixPath(handle.filename).suffix.lower()
        kind, content_type, language, limit = "unsupported", "application/octet-stream", None, None
        if extension == ".pdf":
            kind, content_type, limit = "pdf", "application/pdf", MAX_PDF_BYTES
        elif extension in {".png", ".jpg", ".jpeg"}:
            kind = "image"
            content_type = "image/png" if extension == ".png" else "image/jpeg"
            limit = MAX_IMAGE_BYTES
        elif extension in {".md", ".markdown"}:
            kind, content_type, language, limit = "markdown", "text/plain", "markdown", MAX_TEXT_BYTES
        elif extension in TEXT_LANGUAGES:
            kind, content_type, language, limit = "text", "text/plain", TEXT_LANGUAGES[extension], MAX_TEXT_BYTES

        reason = None
        if kind == "unsupported":
            reason = "此文件类型暂不支持预览，可下载后查看"
        elif handle.size_bytes > limit:
            reason = f"文件超过 {limit // (1024 * 1024)} MiB 预览限制，可下载后查看"
        elif kind in {"text", "markdown"}:
            try:
                read_preview_text(handle)
            except TextPreviewError as exc:
                reason = str(exc)
        else:
            prefix = os.pread(handle.fd, 16, 0)
            valid = (
                prefix.startswith(b"%PDF-") if kind == "pdf"
                else prefix.startswith(b"\x89PNG\r\n\x1a\n") if extension == ".png"
                else prefix.startswith(b"\xff\xd8\xff")
            )
            if not valid:
                reason = "文件内容与格式不符，可下载后查看"

        return handle, {
            "filename": handle.filename,
            "kind": kind,
            "content_type": content_type,
            "size_bytes": handle.size_bytes,
            "language": language,
            "can_preview": reason is None,
            "reason": reason,
            "limit_bytes": limit,
        }
    except OSError as exc:
        handle.close()
        raise DownloadAccessError("文件读取失败") from exc
    except Exception:
        handle.close()
        raise


def parse_byte_range(value: str, size: int) -> tuple[int, int]:
    """支持单段、开放结尾及后缀 Range；拒绝多段和无效范围。"""
    if len(value) > 128 or size <= 0:
        raise ValueError("无效的字节范围")
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or not any(match.groups()):
        raise ValueError("无效的字节范围")
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if suffix <= 0:
            raise ValueError("无效的字节范围")
        return max(0, size - suffix), size - 1
    start, end = int(first), int(last) if last else size - 1
    if start >= size or start > end:
        raise ValueError("无效的字节范围")
    return start, min(end, size - 1)


def iter_preview_chunks(handle: DownloadHandle, start: int, length: int):
    """分块读取已验证描述符，客户端断开或读完后关闭描述符。"""
    try:
        offset, remaining = start, length
        while remaining > 0:
            chunk = os.pread(handle.fd, min(CHUNK_SIZE, remaining), offset)
            if not chunk:
                break
            offset += len(chunk)
            remaining -= len(chunk)
            yield chunk
    finally:
        handle.close()
