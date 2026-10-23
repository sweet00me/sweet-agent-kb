
/**
 * 文件：src/components/files/FileBrowser.tsx
 *
 * 用途：
 *   浏览 Sweet Research Agent 服务器允许访问的文件。
 *
 * 主要功能：
 *   1. 浏览文件夹和文件。
 *   2. 支持目录面包屑导航。
 *   3. 使用后端接口分页。
 *   4. 根据文件名递归搜索。
 *   5. 下载选中的文件。
 *   6. 图片使用 56 像素缩略图，进入可视区域附近才加载，失败回退原图标。
 *
 * 安全原则：
 *   1. 不拼接 Linux 绝对路径。
 *   2. root_id 来自后端存储根目录列表。
 *   3. 不执行删除、覆盖或移动操作。
 *   4. 切换目录时忽略过期请求响应。
 */

import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from 'react'

import {
  Folder,
  FileText,
  Download,
  Search,
  ChevronRight,
  ChevronLeft,
  RefreshCw,
  AlertCircle,
  Eye,
} from 'lucide-react'

import { FilePreview } from './FilePreview'

import {
  browseFiles,
  searchFiles,
  getFileDownloadUrl,
  getFileThumbnailUrl,
} from '../../services/files'

import type {
  StorageRootInfo,
  BrowseFilesResponse,
  SearchFilesResponse,
  FileEntry,
} from '../../types/files'


type BrowserPurpose = 'browser' | 'download' | 'search'
type DisplayMode = 'browse' | 'search'

interface FileBrowserProps {
  root: StorageRootInfo
  purpose: BrowserPurpose
  initialQuery?: string
  onBack: () => void
}

const PAGE_SIZE = 100
const SEARCH_LIMIT = 50
const IMAGE_FILENAME = /\.(jpe?g|png|webp|gif|bmp)$/i

/** 图片仅加载受限缩略图；不支持 IntersectionObserver 时使用浏览器原生懒加载。 */
function FileThumbnail({ url }: { url: string }) {
  const container = useRef<HTMLSpanElement>(null)
  const [nearViewport, setNearViewport] = useState(() => typeof IntersectionObserver === 'undefined')
  const [loaded, setLoaded] = useState(false)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    const element = container.current
    if (!element || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) {
        setNearViewport(true)
        observer.disconnect()
      }
    }, { rootMargin: '200px' })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  return (
    <span ref={container} aria-hidden="true"
      className="relative flex h-14 w-14 shrink-0 items-center justify-center overflow-hidden rounded-lg bg-slate-100">
      {!loaded && <FileText size={20} className="text-slate-600" />}
      {nearViewport && !failed && (
        <img src={url} alt="" width={56} height={56} loading="lazy" decoding="async"
          className={`absolute inset-0 h-full w-full object-cover ${loaded ? '' : 'opacity-0'}`}
          onLoad={() => setLoaded(true)} onError={() => setFailed(true)} />
      )}
    </span>
  )
}


// =====================================
// 1. 文件信息格式化
// =====================================

function formatFileSize(
  size: number | null,
): string {
  if (size === null) return '文件夹'

  if (size < 1024) {
    return `${size} B`
  }

  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`
  }

  if (size < 1024 * 1024 * 1024) {
    return `${(
      size / (1024 * 1024)
    ).toFixed(1)} MB`
  }

  return `${(
    size / (1024 * 1024 * 1024)
  ).toFixed(2)} GB`
}

function formatTime(value: string | null): string {
  if (!value) return '修改时间未知'

  const date = new Date(value)

  if (Number.isNaN(date.getTime())) {
    return '修改时间未知'
  }

  return date.toLocaleString('zh-CN', {
    hour12: false,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}


// =====================================
// 2. 文件浏览器
// =====================================

export function FileBrowser({
  root,
  purpose,
  initialQuery = '',
  onBack,
}: FileBrowserProps) {
  const initialSearch = initialQuery.trim()

  const [currentPath, setCurrentPath] = useState('')
  const [offset, setOffset] = useState(0)

  const [displayMode, setDisplayMode] =
    useState<DisplayMode>(
      purpose === 'search' && initialSearch
        ? 'search'
        : 'browse',
    )

  const [searchInput, setSearchInput] =
    useState(initialSearch)

  const [submittedQuery, setSubmittedQuery] =
    useState(initialSearch)

  const [listing, setListing] =
    useState<BrowseFilesResponse | null>(null)

  const [searchResult, setSearchResult] =
    useState<SearchFilesResponse | null>(null)

  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [reloadKey, setReloadKey] = useState(0)
  const [previewFile, setPreviewFile] = useState<FileEntry | null>(null)


  // =====================================
  // 3. 请求真实文件数据
  // =====================================

  useEffect(() => {
    let cancelled = false

    async function loadFiles() {
      setLoading(true)
      setError('')

      try {
        if (displayMode === 'search' && submittedQuery) {
          const response = await searchFiles({
            rootId: root.root_id,
            path: currentPath,
            query: submittedQuery,
            recursive: true,
            limit: SEARCH_LIMIT,
            maxDepth: 5,
          })

          if (cancelled) return

          setSearchResult(response)
        } else {
          const response = await browseFiles({
            rootId: root.root_id,
            path: currentPath,
            limit: PAGE_SIZE,
            offset,
          })

          if (cancelled) return

          setListing(response)
        }
      } catch (err) {
        if (cancelled) return

        console.error('读取服务器文件失败:', err)

        setError(
          err instanceof Error
            ? err.message
            : '服务器文件读取失败',
        )
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    }

    void loadFiles()

    return () => {
      cancelled = true
    }
  }, [
    root.root_id,
    currentPath,
    displayMode,
    submittedQuery,
    offset,
    reloadKey,
  ])


  // =====================================
  // 4. 目录导航
  // =====================================

  function openDirectory(path: string) {
    setCurrentPath(path)
    setOffset(0)
    setDisplayMode('browse')
    setSubmittedQuery('')
    setListing(null)
    setSearchResult(null)
    setError('')
    setReloadKey((previous) => previous + 1)
  }

  function goToParent() {
    const parent =
      displayMode === 'browse' && listing
        ? listing.parent_path
        : currentPath.split('/').slice(0, -1).join('/')

    if (parent !== null) {
      openDirectory(parent)
    }
  }


  // =====================================
  // 5. 文件搜索
  // =====================================

  function handleSearch(
    event: FormEvent<HTMLFormElement>,
  ) {
    event.preventDefault()

    const keyword = searchInput.trim()

    setOffset(0)
    setSubmittedQuery(keyword)
    setDisplayMode(keyword ? 'search' : 'browse')

    // 相同关键词也可以重新搜索。
    setReloadKey((previous) => previous + 1)
  }


  // =====================================
  // 6. 页面数据
  // =====================================

  const isSearching =
    displayMode === 'search' && Boolean(submittedQuery)

  const items: FileEntry[] = isSearching
    ? searchResult?.items ?? []
    : listing?.items ?? []

  const pathParts = currentPath
    ? currentPath.split('/').filter(Boolean)
    : []

  const canGoParent = currentPath.length > 0

  const title =
    purpose === 'download'
      ? '文件下载'
      : purpose === 'search'
        ? '文件搜索'
        : '文档管理'


  // =====================================
  // 7. 页面渲染
  // =====================================

  return (
    <div className="min-w-0 space-y-5">

      {/* 顶部 */}
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={onBack}
          aria-label="返回知识库首页"
          className="rounded-xl border border-slate-200 p-2"
        >
          <ChevronLeft size={22} />
        </button>

        <div className="min-w-0 flex-1">
          <h2 className="text-lg font-semibold">
            {title}
          </h2>

          <p className="truncate text-xs text-slate-500">
            {root.label}
          </p>
        </div>

        <button
          type="button"
          onClick={() =>
            setReloadKey((previous) => previous + 1)
          }
          aria-label="刷新文件列表"
          className="rounded-xl border border-slate-200 p-2"
        >
          <RefreshCw size={19} />
        </button>
      </div>


      {/* 搜索 */}
      <form
        role="search"
        onSubmit={handleSearch}
        className="flex min-w-0 gap-2"
      >
        <div className="flex min-w-0 flex-1 items-center gap-2 rounded-xl border border-slate-200 bg-white px-3">
          <Search
            size={19}
            className="shrink-0 text-slate-400"
          />

          <input
            type="search"
            aria-label="搜索当前目录"
            value={searchInput}
            maxLength={100}
            onChange={(event) =>
              setSearchInput(event.target.value)
            }
            placeholder="搜索当前目录及子目录"
            className="min-w-0 flex-1 bg-transparent py-3 text-sm outline-none"
          />
        </div>

        <button
          type="submit"
          disabled={loading}
          className="rounded-xl bg-slate-900 px-4 text-sm text-white disabled:opacity-50"
        >
          搜索
        </button>
      </form>


      {/* 面包屑导航 */}
      <nav
        aria-label="目录路径"
        className="flex flex-wrap items-center gap-1 text-xs"
      >
        <button
          type="button"
          onClick={() => openDirectory('')}
          className="rounded-lg px-2 py-1.5 text-slate-600 hover:bg-slate-100"
        >
          {root.label}
        </button>

        {pathParts.map((part, index) => {
          const path = pathParts
            .slice(0, index + 1)
            .join('/')

          return (
            <div
              key={path}
              className="flex min-w-0 items-center gap-1"
            >
              <ChevronRight
                size={14}
                className="shrink-0 text-slate-400"
              />

              <button
                type="button"
                onClick={() => openDirectory(path)}
                className="max-w-40 truncate rounded-lg px-2 py-1.5 text-slate-700 hover:bg-slate-100"
                title={part}
              >
                {part}
              </button>
            </div>
          )
        })}
      </nav>


      {/* 文件列表 */}
      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white">

        <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
          <div>
            <p className="text-sm font-semibold">
              {isSearching ? '搜索结果' : '目录内容'}
            </p>

            <p className="mt-1 text-xs text-slate-500">
              {loading
                ? '正在读取...'
                : isSearching
                  ? `返回 ${searchResult?.returned ?? 0} 项`
                  : `共 ${listing?.total ?? 0} 项`}
            </p>
          </div>

          {canGoParent && (
            <button
              type="button"
              onClick={goToParent}
              className="text-xs font-medium text-slate-600"
            >
              返回上级
            </button>
          )}
        </div>


        {error && (
          <div
            role="alert"
            className="flex gap-2 border-b border-rose-100 bg-rose-50 px-4 py-3 text-sm text-rose-700"
          >
            <AlertCircle
              size={18}
              className="mt-0.5 shrink-0"
            />
            <span className="break-words">{error}</span>
          </div>
        )}


        {loading ? (
          <div
            role="status"
            className="px-4 py-12 text-center text-sm text-slate-500"
          >
            正在加载服务器文件...
          </div>
        ) : error ? null : items.length === 0 ? (
          <div className="px-4 py-12 text-center text-sm text-slate-500">
            {isSearching
              ? '没有找到匹配的文件'
              : '当前目录为空'}
          </div>
        ) : (
          <div className="divide-y divide-slate-100">
            {items.map((item) => {
              const isDirectory =
                item.kind === 'directory'

              return (
                <div
                  key={item.path}
                  className="flex min-w-0 items-center gap-3 px-4 py-3"
                >
                  {!isDirectory && IMAGE_FILENAME.test(item.name) ? (
                    <FileThumbnail
                      key={`${root.root_id}:${item.path}:${item.modified_at}:${item.size_bytes}:${reloadKey}`}
                      url={getFileThumbnailUrl({ rootId: root.root_id, path: item.path },
                        `${item.modified_at}:${item.size_bytes}:${reloadKey}`)} />
                  ) : (
                  <div className="shrink-0 rounded-lg bg-slate-100 p-2.5">
                    {isDirectory ? (
                      <Folder
                        size={20}
                        className="text-slate-600"
                      />
                    ) : (
                      <FileText
                        size={20}
                        className="text-slate-600"
                      />
                    )}
                  </div>
                  )}

                  <div className="min-w-0 flex-1">
                    {isDirectory ? (
                      <button
                        type="button"
                        onClick={() =>
                          openDirectory(item.path)
                        }
                        className="block max-w-full break-all text-left text-sm font-medium text-slate-900"
                      >
                        {item.name}
                      </button>
                    ) : (
                      <button type="button" onClick={() => setPreviewFile(item)}
                        className="block max-w-full break-all text-left text-sm font-medium text-slate-900 hover:underline">
                        {item.name}
                      </button>
                    )}

                    {isSearching && (
                      <p className="mt-1 break-all text-xs text-slate-400">
                        {item.path}
                      </p>
                    )}

                    <p className="mt-1 text-xs leading-5 text-slate-400">
                      {formatFileSize(item.size_bytes)}
                      <span className="mx-1.5">·</span>
                      {formatTime(item.modified_at)}
                    </p>
                  </div>

                  {isDirectory ? (
                    <button
                      type="button"
                      onClick={() =>
                        openDirectory(item.path)
                      }
                      aria-label={`打开文件夹 ${item.name}`}
                      className="shrink-0 rounded-lg p-2"
                    >
                      <ChevronRight
                        size={19}
                        className="text-slate-400"
                      />
                    </button>
                  ) : (
                    <div className="flex shrink-0 items-center gap-2">
                    <button type="button" onClick={() => setPreviewFile(item)}
                      aria-label={`预览 ${item.name}`} title={`预览 ${item.name}`}
                      className="rounded-lg border border-slate-200 p-2.5 text-slate-700 hover:bg-slate-50">
                      <Eye size={19} />
                    </button>
                    <a
                      href={getFileDownloadUrl({
                        rootId: root.root_id,
                        path: item.path,
                      })}
                      download={item.name}
                      aria-label={`下载 ${item.name}`}
                      title={`下载 ${item.name}`}
                      className="shrink-0 rounded-lg border border-slate-200 p-2.5 text-slate-700 hover:bg-slate-50"
                    >
                      <Download size={19} />
                    </a>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>


      {/* 搜索结果限制提示 */}
      {isSearching && searchResult && !loading && (
        <div className="space-y-2">
          {searchResult.incomplete && (
            <div
              role="status"
              className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs leading-6 text-amber-900"
            >
              搜索未完全结束，当前只展示部分结果。
              可以缩小搜索目录或使用更具体的关键词。
              {searchResult.stop_reason && (
                <p className="mt-1 break-all">
                  限制原因：{searchResult.stop_reason}
                </p>
              )}
            </div>
          )}

          {searchResult.depth_limited && (
            <p className="text-xs text-slate-500">
              部分更深层级目录未被搜索。
            </p>
          )}

          {searchResult.skipped_directories > 0 && (
            <p className="text-xs text-slate-500">
              跳过目录：{searchResult.skipped_directories} 个。
            </p>
          )}
        </div>
      )}


      {/* 目录分页 */}
      {!isSearching && listing && !loading && !error && (
        <div className="flex items-center justify-between gap-3">
          <p className="text-xs text-slate-500">
            {listing.total === 0
              ? '暂无文件'
              : `${listing.offset + 1}–${
                  listing.offset + listing.items.length
                } / ${listing.total}`}
          </p>

          <div className="flex gap-2">
            <button
              type="button"
              disabled={offset === 0}
              onClick={() =>
                setOffset((previous) =>
                  Math.max(0, previous - PAGE_SIZE)
                )
              }
              className="rounded-lg border border-slate-200 px-3 py-2 text-xs disabled:opacity-40"
            >
              上一页
            </button>

            <button
              type="button"
              disabled={!listing.has_more}
              onClick={() =>
                setOffset((previous) =>
                  previous + PAGE_SIZE
                )
              }
              className="rounded-lg border border-slate-200 px-3 py-2 text-xs disabled:opacity-40"
            >
              下一页
            </button>
          </div>
        </div>
      )}
      {previewFile && (
        <FilePreview key={`${root.root_id}:${previewFile.path}`} rootId={root.root_id}
          file={previewFile} onClose={() => setPreviewFile(null)} />
      )}
    </div>
  )
}
