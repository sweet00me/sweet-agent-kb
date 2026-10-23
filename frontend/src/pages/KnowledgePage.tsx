
/**
 * 文件：src/pages/KnowledgePage.tsx
 *
 * 用途：
 *   Sweet Research Agent 知识库文件管理首页。
 *
 * 主要功能：
 *   1. 获取服务器存储根目录。
 *   2. 搜索服务器文件。
 *   3. 进入文档管理页面。
 *   4. 进入文件下载页面。
 *   5. 预留文件上传入口。
 *
 * 当前阶段：
 *   浏览、搜索、下载使用真实 FastAPI。
 *   上传功能等待后端接口完成。
 *
 * 不影响 Agent 聊天、SSE 与 HITL。
 */

import {
  useEffect,
  useState,
  type FormEvent,
} from 'react'

import {
  Search,
  FolderOpen,
  Upload,
  Download,
  ChevronRight,
  HardDrive,
} from 'lucide-react'

import { getStorageRoots } from '../services/files'
import { FileBrowser } from '../components/files/FileBrowser'

import type { StorageRootInfo } from '../types/files'
import { FileUpload } from '../components/files/FileUpload'


type FileView =
  | 'home'
  | 'browser'
  | 'download'
  | 'search'
  | 'upload'


export function KnowledgePage() {
  const [view, setView] = useState<FileView>('home')

  const [roots, setRoots] = useState<StorageRootInfo[]>([])
  const [rootId, setRootId] = useState('')

  const [rootsLoading, setRootsLoading] = useState(true)
  const [rootsError, setRootsError] = useState('')

  const [searchQuery, setSearchQuery] = useState('')
  const [activeQuery, setActiveQuery] = useState('')

  const [notice, setNotice] = useState('')

  const selectedRoot =
    roots.find((root) => root.root_id === rootId) ?? null


  // =====================================
  // 1. 加载后端存储根目录
  // =====================================

  useEffect(() => {
    let cancelled = false

    getStorageRoots()
      .then((response) => {
        if (cancelled) return

        setRoots(response.items)

        setRootId((previous) => {
          if (
            response.items.some(
              (root) => root.root_id === previous,
            )
          ) {
            return previous
          }

          return response.items[0]?.root_id ?? ''
        })
      })
      .catch((error: unknown) => {
        if (cancelled) return

        console.error('获取存储根目录失败:', error)
        setRootsError('无法获取服务器存储目录，请检查 API。')
      })
      .finally(() => {
        if (!cancelled) {
          setRootsLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [])


  // =====================================
  // 2. 搜索文件
  // =====================================

  function handleSearch(
    event: FormEvent<HTMLFormElement>,
  ) {
    event.preventDefault()

    const keyword = searchQuery.trim()

    if (!keyword) {
      setNotice('请输入文件搜索关键词。')
      return
    }

    if (!selectedRoot) {
      setNotice('请先选择可用的存储根目录。')
      return
    }

    setActiveQuery(keyword)
    setView('search')
    setNotice('')
  }


  // =====================================
  // 3. 打开功能页面
  // =====================================

  function openPage(nextView: 'browser' | 'download') {
    if (!selectedRoot) {
      setNotice('当前没有可用的存储根目录。')
      return
    }

    setActiveQuery('')
    setView(nextView)
    setNotice('')
  }


  // =====================================
  // 4. 页面渲染
  // =====================================

  return (
    <section className="min-h-0 flex-1 overflow-y-auto px-4 py-6">
      <div className="mx-auto w-full max-w-3xl">

        <div className="mb-6">
          <h1 className="text-2xl font-semibold">
            我的知识库
          </h1>

          <p className="mt-2 text-sm text-slate-500">
            管理个人论文、科研笔记和实验资料。
          </p>
        </div>


        {/* 存储位置选择 */}
        <div className="mb-6">
          <label
            htmlFor="storage-root"
            className="mb-2 flex items-center gap-2 text-sm font-medium text-slate-600"
          >
            <HardDrive size={17} />
            存储位置
          </label>

          <select
            id="storage-root"
            value={rootId}
            disabled={rootsLoading || roots.length === 0}
            onChange={(event) => {
              setRootId(event.target.value)
              setNotice('')
            }}
            className="min-h-12 w-full rounded-xl border border-slate-200 bg-white px-3 text-sm text-slate-900 outline-none focus:border-slate-400 disabled:opacity-50"
          >
            {roots.length === 0 && (
              <option value="">
                {rootsLoading
                  ? '正在加载存储位置...'
                  : '没有可用的存储位置'}
              </option>
            )}

            {roots.map((root) => (
              <option
                key={root.root_id}
                value={root.root_id}
              >
                {root.label}
              </option>
            ))}
          </select>

          {rootsError && (
            <p
              role="alert"
              className="mt-2 text-xs text-rose-600"
            >
              {rootsError}
            </p>
          )}
        </div>


        {/* =====================================
            文档管理 / 搜索 / 下载
        ===================================== */}

        {view === 'upload' ? (
          <FileUpload
            onBack={() => setView('home')}
          />
        ) : view !== 'home' ? (
          selectedRoot ? (
            <FileBrowser
              key={`${view}:${rootId}:${activeQuery}`}
              root={selectedRoot}
              purpose={view}
              initialQuery={
                view === 'search' ? activeQuery : ''
              }
              onBack={() => setView('home')}
            />
          ) : (
            <p className="text-sm text-slate-500">
              没有可用的存储根目录。
            </p>
          )
        ) : (
          <>
            {/* 文件搜索 */}
            <form
              role="search"
              onSubmit={handleSearch}
              className="mb-8"
            >
              <div className="flex min-h-14 items-center gap-3 rounded-2xl border border-slate-200 bg-white px-4 shadow-sm focus-within:border-slate-400">
                <Search
                  size={21}
                  className="shrink-0 text-slate-400"
                />

                <input
                  type="search"
                  aria-label="搜索服务器文件"
                  value={searchQuery}
                  maxLength={100}
                  onChange={(event) =>
                    setSearchQuery(event.target.value)
                  }
                  placeholder="搜索文件名称、科研资料..."
                  className="min-w-0 flex-1 bg-transparent py-4 text-sm outline-none placeholder:text-slate-400"
                />

                <button
                  type="submit"
                  disabled={
                    !searchQuery.trim() || !selectedRoot
                  }
                  className="shrink-0 rounded-lg bg-slate-900 px-3 py-2 text-xs text-white disabled:opacity-40"
                >
                  搜索
                </button>
              </div>

              <p className="mt-2 px-1 text-xs text-slate-400">
                在所选存储位置中搜索文件和文件夹
              </p>
            </form>


            {/* 文件管理功能 */}
            <h2 className="mb-3 text-sm font-semibold text-slate-500">
              文件管理
            </h2>

            <div className="space-y-3">

              {/* 文档管理 */}
              <button
                type="button"
                onClick={() => openPage('browser')}
                disabled={!selectedRoot}
                className="flex min-h-20 w-full items-center gap-4 rounded-2xl border border-slate-200 bg-white p-4 text-left hover:bg-slate-50 active:bg-slate-100 disabled:opacity-50"
              >
                <div className="shrink-0 rounded-xl bg-slate-100 p-3">
                  <FolderOpen size={23} />
                </div>

                <div className="min-w-0 flex-1">
                  <p className="font-medium">
                    文档管理
                  </p>

                  <p className="mt-1 text-xs leading-5 text-slate-400">
                    浏览服务器目录与科研文件
                  </p>
                </div>

                <ChevronRight
                  size={19}
                  className="shrink-0 text-slate-400"
                />
              </button>


              <button
                type="button"
                onClick={() => {
                  setView('upload')
                  setNotice('')
                }}
                className="flex min-h-20 w-full items-center gap-4 rounded-2xl border border-slate-200 bg-white p-4 text-left transition-colors hover:bg-slate-50 active:bg-slate-100"
              >
                <div className="shrink-0 rounded-xl bg-slate-100 p-3">
                  <Upload size={23} />
                </div>

                <div className="min-w-0 flex-1">
                  <p className="font-medium">
                    文件上传
                  </p>

                  <p className="mt-1 text-xs leading-5 text-slate-400">
                    上传本地科研文件到服务器
                  </p>
                </div>

                <ChevronRight
                  size={19}
                  className="shrink-0 text-slate-400"
                />
              </button>


              {/* 文件下载 */}
              <button
                type="button"
                onClick={() => openPage('download')}
                disabled={!selectedRoot}
                className="flex min-h-20 w-full items-center gap-4 rounded-2xl border border-slate-200 bg-white p-4 text-left hover:bg-slate-50 active:bg-slate-100 disabled:opacity-50"
              >
                <div className="shrink-0 rounded-xl bg-slate-100 p-3">
                  <Download size={23} />
                </div>

                <div className="min-w-0 flex-1">
                  <p className="font-medium">
                    文件下载
                  </p>

                  <p className="mt-1 text-xs leading-5 text-slate-400">
                    选择服务器文件并下载到本地
                  </p>
                </div>

                <ChevronRight
                  size={19}
                  className="shrink-0 text-slate-400"
                />
              </button>
            </div>


            {notice && (
              <p
                role="status"
                className="mt-5 rounded-xl bg-amber-50 px-4 py-3 text-sm text-amber-900"
              >
                {notice}
              </p>
            )}
          </>
        )}

      </div>
    </section>
  )
}
