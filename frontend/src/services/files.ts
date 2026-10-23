
/**
 * 文件：src/services/files.ts
 *
 * 用途：
 *   Sweet Research Agent 文件管理 API 服务。
 *
 * 支持：
 *   1. 存储根目录查询。
 *   2. 文件及目录浏览。
 *   3. 递归文件搜索。
 *   4. 构建文件下载地址。
 *   5. 构建独立缩略图地址，版本参数仅用于前端刷新，不替代后端校验。
 *
 * 说明：
 *   所有文件操作通过 FastAPI 提供的
 *   root_id 和相对路径进行。
 *
 *   当前不执行任何服务器端写入或删除。
 */

import { apiRequest, ApiError } from './api'

import type {
  StorageRootsResponse,
  BrowseFilesParams,
  BrowseFilesResponse,
  SearchFilesParams,
  SearchFilesResponse,
  DownloadFileParams,
  FilePreviewInfo,
} from '../types/files'


// =====================================
// 1. 获取存储根目录
// =====================================

export async function getStorageRoots():
  Promise<StorageRootsResponse> {
  return apiRequest<StorageRootsResponse>(
    '/api/v1/files/roots',
  )
}


// =====================================
// 2. 浏览目录
// =====================================

export async function browseFiles({
  rootId,
  path = '',
  limit = 100,
  offset = 0,
}: BrowseFilesParams): Promise<BrowseFilesResponse> {
  const params = new URLSearchParams({
    root_id: rootId,
    path,
    limit: String(limit),
    offset: String(offset),
  })

  return apiRequest<BrowseFilesResponse>(
    `/api/v1/files?${params.toString()}`,
  )
}


// =====================================
// 3. 搜索文件
// =====================================

export async function searchFiles({
  rootId,
  query,
  path = '',
  recursive = true,
  limit = 50,
  maxDepth = 5,
}: SearchFilesParams): Promise<SearchFilesResponse> {
  const params = new URLSearchParams({
    root_id: rootId,
    query,
    path,
    recursive: String(recursive),
    limit: String(limit),
    max_depth: String(maxDepth),
  })

  return apiRequest<SearchFilesResponse>(
    `/api/v1/files/search?${params.toString()}`,
  )
}


// =====================================
// 4. 获取下载地址
// =====================================

export function getFileDownloadUrl({
  rootId,
  path,
}: DownloadFileParams): string {
  const params = new URLSearchParams({
    root_id: rootId,
    path,
  })

  return `/api/v1/files/download?${params.toString()}`
}

export function getFilePreviewUrl({ rootId, path }: DownloadFileParams): string {
  const params = new URLSearchParams({ root_id: rootId, path })
  return `/api/v1/files/preview?${params.toString()}`
}

export function getFileThumbnailUrl(
  { rootId, path }: DownloadFileParams,
  version: string,
): string {
  const params = new URLSearchParams({ root_id: rootId, path, v: version })
  return `/api/v1/files/thumbnail?${params.toString()}`
}

export function getFilePreviewInfo(
  { rootId, path }: DownloadFileParams,
  signal?: AbortSignal,
): Promise<FilePreviewInfo> {
  const params = new URLSearchParams({ root_id: rootId, path })
  return apiRequest<FilePreviewInfo>(`/api/v1/files/preview/info?${params}`, { signal })
}

export async function getFilePreviewText(
  params: DownloadFileParams,
  signal?: AbortSignal,
): Promise<string> {
  const response = await fetch(getFilePreviewUrl(params), { signal })
  if (!response.ok) throw new ApiError(response.status, '无法读取文件内容，请下载后查看。')
  // JSON 文件也作为原始文本返回，避免被通用 JSON 请求函数解析为对象。
  return response.text()
}

/**
 * 上传单个文件。
 *
 * 对应：
 * POST /api/v1/files/upload
 *
 * 重要：
 *   1. 后端固定保存到 Storage/PC_Files/Uploads。
 *   2. 不允许覆盖同名文件。
 *   3. 不手动设置 Content-Type。
 *      浏览器会自动生成 multipart boundary。
 *   4. 不自动重试上传。
 *
 * 当前后端成功响应尚未定义具体 TypeScript 模型，
 * 因此先以 unknown 接收。
 */
export async function uploadFile(
  file: File,
): Promise<unknown> {
  const formData = new FormData()

  formData.append(
    'file',
    file,
    file.name,
  )

  return apiRequest<unknown>(
    '/api/v1/files/upload',
    {
      method: 'POST',
      body: formData,
    },
  )
}
