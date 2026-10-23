import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { Download, FileQuestion, Maximize2, Minimize2, X } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { getFileDownloadUrl, getFilePreviewInfo, getFilePreviewText, getFilePreviewUrl } from '../../services/files'
import type { FileEntry, FilePreviewInfo } from '../../types/files'

const PdfPreview = lazy(() => import('./PdfPreview'))

function ImagePreview({ url, filename }: { url: string; filename: string }) {
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  return (
    <div className="relative flex min-h-0 flex-1 items-center justify-center overflow-auto bg-slate-100 p-4">
      {loading && <p role="status" className="absolute text-sm text-slate-500">正在加载图片…</p>}
      {failed ? <p role="alert" className="text-sm text-red-600">无法显示图片，可下载后查看。</p> : (
        <img src={url} alt={filename} decoding="async" className="max-h-full max-w-full object-contain"
          onLoad={() => setLoading(false)} onError={() => { setLoading(false); setFailed(true) }} />
      )}
    </div>
  )
}

export function FilePreview({ rootId, file, onClose }: {
  rootId: string
  file: FileEntry
  onClose: () => void
}) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const [info, setInfo] = useState<FilePreviewInfo | null>(null)
  const [text, setText] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [fullScreen, setFullScreen] = useState(false)
  const params = { rootId, path: file.path }
  const downloadUrl = getFileDownloadUrl(params)
  const previewUrl = getFilePreviewUrl(params)

  useEffect(() => {
    const dialog = dialogRef.current
    dialog?.showModal()
    return () => dialog?.close()
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    async function load() {
      try {
        const params = { rootId, path: file.path }
        const metadata = await getFilePreviewInfo(params, controller.signal)
        if (controller.signal.aborted) return
        setInfo(metadata)
        if (metadata.can_preview && ['markdown', 'text'].includes(metadata.kind)) {
          const content = await getFilePreviewText(params, controller.signal)
          if (!controller.signal.aborted) setText(content)
        }
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '预览加载失败，请下载后查看。')
      } finally {
        if (!controller.signal.aborted) setLoading(false)
      }
    }
    void load()
    return () => controller.abort()
  }, [rootId, file.path])

  return (
    <dialog ref={dialogRef} aria-label="文件预览" onCancel={onClose}
      className={`m-0 h-dvh max-h-none w-screen max-w-none border-0 bg-white p-0 text-slate-900 shadow-xl backdrop:bg-black/40 ${
        fullScreen ? '' : 'sm:m-auto sm:h-[90dvh] sm:w-[calc(100%_-_2rem)] sm:max-w-6xl sm:rounded-2xl'
      }`}>
      <div className="flex h-full min-h-0 flex-col pb-[env(safe-area-inset-bottom)]">
        <header className="flex shrink-0 items-center gap-2 border-b border-slate-200 px-4 pb-3"
          style={{ paddingTop: 'max(0.75rem, env(safe-area-inset-top))' }}>
          <div className="min-w-0 flex-1">
            <h2 title={file.name} className="truncate text-sm font-semibold">{file.name}</h2>
            {info?.language && <p className="mt-0.5 text-xs text-slate-400">{info.language}</p>}
          </div>
          <a href={downloadUrl} download={file.name} aria-label={`下载 ${file.name}`} title="下载文件"
            className="rounded-lg p-2 hover:bg-slate-100"><Download size={20} /></a>
          <button type="button" onClick={() => setFullScreen(previous => !previous)}
            aria-label={fullScreen ? '退出全屏预览' : '全屏预览'} title={fullScreen ? '退出全屏' : '全屏预览'}
            className="hidden rounded-lg p-2 hover:bg-slate-100 sm:inline-flex">
            {fullScreen ? <Minimize2 size={20} /> : <Maximize2 size={20} />}
          </button>
          <button type="button" onClick={onClose} aria-label="关闭预览" title="关闭预览"
            className="rounded-lg p-2 hover:bg-slate-100"><X size={22} /></button>
        </header>

        {loading ? <p role="status" className="p-8 text-center text-sm text-slate-500">正在加载预览…</p>
          : error || !info?.can_preview ? (
            <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
              <FileQuestion size={36} className="text-slate-400" />
              <p className="font-medium">暂时无法预览此文件</p>
              <p role={error ? 'alert' : 'status'} className="max-w-md break-words text-sm text-slate-500">{error || info?.reason}</p>
              <a href={downloadUrl} download={file.name} className="mt-2 rounded-xl bg-slate-900 px-4 py-2 text-sm text-white">下载文件</a>
            </div>
          ) : info.kind === 'pdf' ? (
            <Suspense fallback={<p role="status" className="p-8 text-center text-sm text-slate-500">正在加载 PDF…</p>}>
              <PdfPreview key={previewUrl} url={previewUrl} />
            </Suspense>
          ) : info.kind === 'image' ? <ImagePreview url={previewUrl} filename={file.name} />
            : info.kind === 'markdown' ? (
              <article className="markdown-content min-h-0 flex-1 overflow-auto px-5 py-3 sm:px-8">
                <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={{
                  table: ({ children }) => <div className="markdown-table-scroll"><table>{children}</table></div>,
                  a: ({ children, href }) => <a href={href} target="_blank" rel="noopener noreferrer" className="text-blue-600 underline">{children}</a>,
                  img: ({ alt }) => <span className="text-slate-500">{alt}</span>,
                }}>{text}</ReactMarkdown>
              </article>
            ) : (
              <pre aria-label="文件文本内容" className="min-h-0 flex-1 overflow-auto bg-slate-50 p-5 text-sm leading-6 text-slate-800"><code>{text || '文件内容为空'}</code></pre>
            )}
      </div>
    </dialog>
  )
}
