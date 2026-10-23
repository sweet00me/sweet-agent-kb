
/**
 * 文件：src/types/files.ts
 *
 * 用途：
 *   定义服务器文件浏览、搜索和下载相关 API 类型。
 *
 * 数据结构：
 *   根据 FastAPI 实际 JSON 响应定义。
 */

export interface StorageRootInfo {
  root_id: string
  label: string
  path: string
  kind: 'home' | 'mount'
  filesystem?: string | null
  writable: boolean
}

export interface StorageRootsResponse {
  count: number
  items: StorageRootInfo[]
}

export type FileKind = 'file' | 'directory'

export interface FileEntry {
  name: string
  path: string
  kind: FileKind
  size_bytes: number | null
  modified_at: string | null
}

export interface BrowseFilesParams {
  rootId: string
  path?: string
  limit?: number
  offset?: number
}

export interface BrowseFilesResponse {
  root_id: string
  path: string
  parent_path: string | null
  total: number
  limit: number
  offset: number
  has_more: boolean
  items: FileEntry[]
}

export interface SearchFilesParams {
  rootId: string
  query: string
  path?: string
  recursive?: boolean
  limit?: number
  maxDepth?: number
}

export interface SearchFilesResponse {
  root_id: string
  path: string
  query: string
  recursive: boolean
  max_depth: number
  scanned_entries: number
  returned: number
  incomplete: boolean
  stop_reason: string | null
  depth_limited: boolean
  skipped_directories: number
  items: FileEntry[]
}

export interface DownloadFileParams {
  rootId: string
  path: string
}

export interface FilePreviewInfo {
  filename: string
  kind: 'pdf' | 'image' | 'markdown' | 'text' | 'unsupported'
  content_type: string
  size_bytes: number
  language: string | null
  can_preview: boolean
  reason: string | null
  limit_bytes: number | null
}
