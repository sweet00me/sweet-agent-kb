
"""
文件：app/api/files.py

用途：
    为 Sweet Research Agent 提供文件管理 HTTP API。

当前功能：
    1. 查询 Linux 主目录。
    2. 查询已挂载的数据磁盘。
    3. 将存储入口转换成统一的 JSON 响应。
    4. 文件夹浏览、搜索、上传、下载和只读预览。
    5. 受限图片缩略图，复用已有文件访问校验并独立缓存。

安全说明：
    文件读取复用目录描述符校验，拒绝路径穿越与符号链接。
    缩略图不改变原文件或上传、下载及预览行为。
    文件根目录 ID 不代表访问授权。
"""

import logging

from typing import Literal

from fastapi import (
    APIRouter,
    HTTPException,
    Response,
    Query,
    Request,
    Header,
)

from pydantic import BaseModel, Field

from app.services.storage import (
    list_storage_roots,
    StorageDiscoveryError,
)

from app.services.file_manager import (
    list_directory,
    UnknownRootError,
    InvalidPathError,
    DirectoryUnavailableError,
    DirectoryTooLargeError,
)
from app.services.storage import StorageDiscoveryError

from app.services.file_search import (
    search_files,
    InvalidSearchQueryError,
)

from urllib.parse import quote

from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.services.file_transfer import (
    DownloadHandle,
    open_download,
    iter_download_chunks,
    DownloadAccessError,
    DownloadNotFoundError,
    DownloadTooLargeError,
)

from app.services.file_preview import (
    open_preview,
    read_preview_text,
    parse_byte_range,
    iter_preview_chunks,
    TextPreviewError,
)
from app.services.file_thumbnail import get_thumbnail, ThumbnailError

from fastapi import File, UploadFile

from app.services.file_upload import (
    save_uploaded_file,
    InvalidUploadError,
    UploadTooLargeError,
    UploadConflictError,
    UploadUnavailableError,
)

logger = logging.getLogger(__name__)


# =====================================
# 1. 创建文件管理路由
# =====================================

router = APIRouter(
    prefix="/api/v1/files",
    tags=["File Management"],
)


# =====================================
# 2. 定义存储入口数据结构
# =====================================

class StorageRootInfo(BaseModel):
    """
    前端可以浏览的一个存储入口。

    root_id：
        唯一标识，后续文件管理接口会使用。

    path：
        Linux 中的真实目录路径。

    writable：
        初步判断根目录是否可写，
        不代表其所有子目录都可写。
    """

    root_id: str
    label: str
    path: str

    kind: Literal["home", "mount"]

    filesystem: str | None = None

    writable: bool


class StorageRootsResponse(BaseModel):
    """
    存储入口查询响应。
    """

    count: int = Field(ge=0)

    items: list[StorageRootInfo]


# =====================================
# 3. 查询存储入口
# =====================================

@router.get(
    "/roots",
    response_model=StorageRootsResponse,
)
def get_storage_roots(
    response: Response,
):
    """
    查询 sweetserver 当前可浏览的存储入口。

    执行流程：
        1. 调用 storage.py 获取挂载信息。
        2. 转换为统一的响应结构。
        3. 将结果返回给前端。

    不执行文件读取、上传或下载。
    """

    # 挂载情况可能变化。
    # 避免客户端长期缓存旧的磁盘列表。
    response.headers["Cache-Control"] = "no-store"

    try:
        roots = list_storage_roots()

    except StorageDiscoveryError as exc:

        logger.error(
            "Storage discovery failed: %s",
            type(exc).__name__,
        )

        raise HTTPException(
            status_code=503,
            detail="服务器存储信息暂时不可用",
        ) from exc

    items = [
        StorageRootInfo(**root)
        for root in roots
    ]

    return StorageRootsResponse(
        count=len(items),
        items=items,
    )


# =====================================
# 4. 浏览指定文件夹
# =====================================

@router.get("")
def browse_files(
    response: Response,
    root_id: str = Query(
        min_length=1,
        max_length=128,
    ),
    path: str = Query(
        default="",
        max_length=1024,
    ),
    limit: int = Query(
        default=100,
        ge=1,
        le=200,
    ),
    offset: int = Query(
        default=0,
        ge=0,
    ),
):
    """
    浏览指定存储入口中的文件夹。

    示例：
        GET /api/v1/files?root_id=home

        GET /api/v1/files
            ?root_id=home
            &path=Workspace

    当前接口只返回文件元数据。
    """

    response.headers["Cache-Control"] = "no-store"

    try:

        return list_directory(
            root_id=root_id,
            relative_path=path,
            limit=limit,
            offset=offset,
        )

    except InvalidPathError as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except UnknownRootError as exc:

        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    except DirectoryUnavailableError as exc:

        raise HTTPException(
            status_code=403,
            detail=str(exc),
        ) from exc

    except DirectoryTooLargeError as exc:

        raise HTTPException(
            status_code=413,
            detail=str(exc),
        ) from exc

    except StorageDiscoveryError as exc:

        logger.error(
            "File browser storage error: %s",
            type(exc).__name__,
        )

        raise HTTPException(
            status_code=503,
            detail="存储信息暂时不可用",
        ) from exc


# =====================================
# 5. 文件名搜索
# =====================================

@router.get("/search")
def search_files_api(
    response: Response,

    root_id: str = Query(
        min_length=1,
        max_length=128,
    ),

    query: str = Query(
        min_length=1,
        max_length=100,
    ),

    path: str = Query(
        default="",
        max_length=1024,
    ),

    recursive: bool = Query(
        default=True,
    ),

    limit: int = Query(
        default=50,
        ge=1,
        le=100,
    ),

    max_depth: int = Query(
        default=5,
        ge=0,
        le=8,
    ),
):
    """
    根据文件名关键词查找文件或目录。

    本接口：
        - 不读取文件正文。
        - 不调用 DeepSeek。
        - 不修改磁盘。
    """

    response.headers["Cache-Control"] = "no-store"

    try:

        return search_files(
            root_id=root_id,
            query=query,
            relative_path=path,
            recursive=recursive,
            limit=limit,
            max_depth=max_depth,
        )

    except (
        InvalidSearchQueryError,
        InvalidPathError,
    ) as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except UnknownRootError as exc:

        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    except DirectoryUnavailableError as exc:

        raise HTTPException(
            status_code=403,
            detail=str(exc),
        ) from exc

    except StorageDiscoveryError as exc:

        logger.error(
            "File search storage error: %s",
            type(exc).__name__,
        )

        raise HTTPException(
            status_code=503,
            detail="存储信息暂时不可用",
        ) from exc


# =====================================
# 6. 下载服务器文件
# =====================================

@router.get("/download")
def download_file(
    root_id: str = Query(
        min_length=1,
        max_length=128,
    ),
    path: str = Query(
        min_length=1,
        max_length=1024,
    ),
):
    """
    下载指定文件。

    特点：
        使用流式响应。
        不调用模型。
        不修改服务器文件。
        默认触发浏览器下载，而非直接执行或渲染。
    """

    try:

        handle = open_download(
            root_id=root_id,
            relative_path=path,
        )

    except InvalidPathError as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except UnknownRootError as exc:

        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    except DownloadNotFoundError as exc:

        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    except DownloadAccessError as exc:

        raise HTTPException(
            status_code=403,
            detail=str(exc),
        ) from exc

    except DownloadTooLargeError as exc:

        raise HTTPException(
            status_code=413,
            detail=str(exc),
        ) from exc

    except StorageDiscoveryError as exc:

        raise HTTPException(
            status_code=503,
            detail="存储信息暂时不可用",
        ) from exc

    # 使用 RFC 5987 格式兼容中文文件名。
    encoded_filename = quote(
        handle.filename,
        safe="",
    )

    headers = {
        "Content-Disposition": (
            "attachment; filename=\"download\"; "
            f"filename*=UTF-8''{encoded_filename}"
        ),
        "Content-Length": str(
            handle.size_bytes
        ),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }

    return StreamingResponse(
        iter_download_chunks(handle),
        media_type="application/octet-stream",
        headers=headers,
        background=BackgroundTask(
            handle.close
        ),
    )

# =====================================
# 7. 只读预览服务器文件
# =====================================

class FilePreviewInfo(BaseModel):
    filename: str
    kind: Literal["pdf", "image", "markdown", "text", "unsupported"]
    content_type: str
    size_bytes: int
    language: str | None
    can_preview: bool
    reason: str | None
    limit_bytes: int | None


class _PreviewStreamingResponse(StreamingResponse):
    """响应头发送失败、客户端断开和取消时也必须关闭已打开的文件。"""

    def __init__(self, handle: DownloadHandle, **kwargs):
        self.handle = handle
        super().__init__(**kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.handle.close()


def _open_preview_or_error(root_id: str, path: str):
    try:
        return open_preview(root_id, path)
    except InvalidPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (UnknownRootError, DownloadNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DownloadAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DownloadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except StorageDiscoveryError as exc:
        raise HTTPException(status_code=503, detail="存储信息暂时不可用") from exc


@router.get("/preview/info", response_model=FilePreviewInfo)
def file_preview_info(
    response: Response,
    root_id: str = Query(min_length=1, max_length=128),
    path: str = Query(min_length=1, max_length=1024),
):
    """查询文件预览类型和大小限制；不支持预览时仍可使用原下载接口。"""
    handle, info = _open_preview_or_error(root_id, path)
    handle.close()
    response.headers["Cache-Control"] = "no-store"
    return info


@router.get("/preview")
@router.head("/preview")
def preview_file(
    request: Request,
    root_id: str = Query(min_length=1, max_length=128),
    path: str = Query(min_length=1, max_length=1024),
    range_header: str | None = Header(default=None, alias="Range"),
):
    """返回只读内联内容；PDF/图片支持 HTTP Range，文本统一返回 UTF-8。"""
    handle, info = _open_preview_or_error(root_id, path)
    try:
        if not info["can_preview"]:
            too_large = info["limit_bytes"] is not None and handle.size_bytes > info["limit_bytes"]
            raise HTTPException(status_code=413 if too_large else 415, detail=info["reason"])
        headers = {
            "Content-Disposition": "inline; filename=\"preview\"; filename*=UTF-8''" + quote(handle.filename, safe=""),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'; base-uri 'none'; frame-ancestors 'self'",
            "Referrer-Policy": "no-referrer",
        }
        if info["kind"] in {"text", "markdown"}:
            try:
                content = read_preview_text(handle).encode("utf-8")
            except TextPreviewError as exc:
                raise HTTPException(status_code=415, detail=str(exc)) from exc
            handle.close()
            headers["Content-Length"] = str(len(content))
            return Response(
                content=b"" if request.method == "HEAD" else content,
                media_type="text/plain; charset=utf-8", headers=headers,
            )

        headers["Accept-Ranges"] = "bytes"
        start, end = 0, handle.size_bytes - 1
        status = 200
        if range_header:
            try:
                start, end = parse_byte_range(range_header, handle.size_bytes)
            except ValueError:
                handle.close()
                return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{handle.size_bytes}"})
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{handle.size_bytes}"
        headers["Content-Length"] = str(end - start + 1)
        if request.method == "HEAD":
            handle.close()
            return Response(status_code=status, media_type=info["content_type"], headers=headers)
        return _PreviewStreamingResponse(
            handle, content=iter_preview_chunks(handle, start, end - start + 1),
            status_code=status, media_type=info["content_type"], headers=headers,
            background=BackgroundTask(handle.close),
        )
    except Exception:
        handle.close()
        raise


# =====================================
# 8. 图片缩略图
# =====================================

@router.get("/thumbnail")
def thumbnail_file(
    root_id: str = Query(min_length=1, max_length=128),
    path: str = Query(min_length=1, max_length=1024),
    if_none_match: str | None = Header(default=None, alias="If-None-Match", max_length=1024),
):
    """小图最长边 160 像素；缓存命中及条件请求也重新校验文件访问权限。"""
    headers = {
        "Cache-Control": "private, no-cache",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'; base-uri 'none'; frame-ancestors 'self'",
        "Referrer-Policy": "no-referrer",
    }
    try:
        thumbnail = get_thumbnail(root_id, path)
    except (InvalidPathError, UnknownRootError, DownloadNotFoundError, DownloadAccessError,
            DownloadTooLargeError, StorageDiscoveryError, ThumbnailError) as exc:
        status = 503
        if isinstance(exc, ThumbnailError):
            status = exc.status_code
        elif isinstance(exc, InvalidPathError):
            status = 400
        elif isinstance(exc, (UnknownRootError, DownloadNotFoundError)):
            status = 404
        elif isinstance(exc, DownloadAccessError):
            status = 403
        elif isinstance(exc, DownloadTooLargeError):
            status = 413
        detail = "存储信息暂时不可用" if isinstance(exc, StorageDiscoveryError) else str(exc)
        raise HTTPException(status_code=status, detail=detail,
                            headers={**headers, "Cache-Control": "no-store"}) from exc
    headers["ETag"] = thumbnail.etag
    tags = {tag.strip().removeprefix("W/") for tag in (if_none_match or "").split(",")}
    if thumbnail.etag in tags or "*" in tags:
        return Response(status_code=304, headers=headers)
    return Response(content=thumbnail.content, media_type="image/jpeg", headers=headers)


# =====================================
# 9. 上传科研文件
# =====================================

@router.post(
    "/upload",
    status_code=201,
)
def upload_file_api(
    file: UploadFile = File(...),
):
    """
    将文件上传到 Storage/PC_Files/Uploads。

    当前不允许客户端选择任意写入路径。
    不覆盖文件，不调用模型。
    """

    try:

        result = save_uploaded_file(
            filename=file.filename,
            source=file.file,
        )

        return result

    except InvalidUploadError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except UploadTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail=str(exc),
        ) from exc

    except UploadConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc

    except UploadUnavailableError as exc:
        logger.error(
            "File upload failed: %s",
            type(exc).__name__,
        )

        raise HTTPException(
            status_code=503,
            detail="文件上传暂时不可用",
        ) from exc
