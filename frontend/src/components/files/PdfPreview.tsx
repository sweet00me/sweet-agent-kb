import { useEffect, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import {
  getDocument,
  GlobalWorkerOptions,
  type PDFDocumentProxy,
  type PDFPageProxy,
  type RenderTask,
} from 'pdfjs-dist/legacy/build/pdf.mjs'
import workerUrl from 'pdfjs-dist/legacy/build/pdf.worker.min.mjs?url'

GlobalWorkerOptions.workerSrc = workerUrl

function PdfCanvas({ pdf, pageNumber }: { pdf: PDFDocumentProxy; pageNumber: number }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const canvasHostRef = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)
  const [rendering, setRendering] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const observer = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)))
    observer.observe(container)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!width || !canvasHostRef.current) return
    // 每次绘制使用独立 Canvas，快速翻页或旋转屏幕不会复用仍在绘制的画布。
    const canvas = document.createElement('canvas')
    canvas.setAttribute('aria-label', `PDF 第 ${pageNumber} 页`)
    canvas.setAttribute('role', 'img')
    canvasHostRef.current.replaceChildren(canvas)
    let cancelled = false
    let page: PDFPageProxy | undefined
    let task: RenderTask | undefined

    async function draw() {
      setRendering(true)
      setError('')
      try {
        page = await pdf.getPage(pageNumber)
        if (cancelled) { page.cleanup(); return }
        const original = page.getViewport({ scale: 1 })
        const viewport = page.getViewport({ scale: Math.max(0.1, (width - 32) / original.width) })
        const outputScale = Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(2_000_000 / (viewport.width * viewport.height)))
        canvas.width = Math.max(1, Math.floor(viewport.width * outputScale))
        canvas.height = Math.max(1, Math.floor(viewport.height * outputScale))
        canvas.style.width = `${viewport.width}px`
        canvas.style.height = `${viewport.height}px`
        canvas.className = 'block bg-white shadow-sm'
        task = page.render({
          canvas,
          viewport,
          transform: outputScale === 1 ? undefined : [outputScale, 0, 0, outputScale, 0, 0],
        })
        await task.promise
        if (!cancelled) setRendering(false)
      } catch (cause) {
        if (!cancelled) {
          setError(cause instanceof Error && cause.name === 'PasswordException'
            ? '此 PDF 需要密码，请下载后打开。' : 'PDF 页面无法显示，可下载后查看。')
          setRendering(false)
        }
      }
    }
    void draw()
    return () => {
      cancelled = true
      task?.cancel()
      canvas.remove()
      const release = () => { page?.cleanup(); canvas.width = 0; canvas.height = 0 }
      if (task) void task.promise.catch(() => {}).then(release)
      else release()
    }
  }, [pdf, pageNumber, width])

  return (
    <div ref={containerRef} className="min-w-0 flex-1 overflow-auto bg-slate-100 py-4">
      {rendering && <p role="status" className="px-4 pb-3 text-center text-sm text-slate-500">正在渲染 PDF 页面…</p>}
      {error && <p role="alert" className="p-4 text-center text-sm text-red-600">{error}</p>}
      <div ref={canvasHostRef} className="flex min-w-0 justify-center" />
    </div>
  )
}

export default function PdfPreview({ url }: { url: string }) {
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null)
  const [pageNumber, setPageNumber] = useState(1)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    const task = getDocument({
      url,
      cMapUrl: '/pdfjs/cmaps/',
      standardFontDataUrl: '/pdfjs/standard_fonts/',
      wasmUrl: '/pdfjs/wasm/',
      isEvalSupported: false,
      disableAutoFetch: true,
      disableStream: true,
      maxImageSize: 20_000_000,
      canvasMaxAreaInBytes: 16 * 1024 * 1024,
    })
    void task.promise.then(document => { if (!cancelled) setPdf(document) }).catch(cause => {
      if (!cancelled) setError(cause instanceof Error && cause.name === 'PasswordException'
        ? '此 PDF 需要密码，请下载后打开。' : '无法读取 PDF，可下载后查看。')
    })
    return () => { cancelled = true; void task.destroy().catch(() => {}) }
  }, [url])

  if (error) return <p role="alert" className="p-6 text-center text-sm text-red-600">{error}</p>
  if (!pdf) return <p role="status" className="p-6 text-center text-sm text-slate-500">正在加载 PDF…</p>

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center justify-center gap-4 border-b border-slate-200 px-3 py-2">
        <button type="button" aria-label="PDF 上一页" disabled={pageNumber <= 1}
          onClick={() => setPageNumber(previous => previous - 1)}
          className="rounded-lg border border-slate-200 p-2 disabled:opacity-30"><ChevronLeft size={18} /></button>
        <p aria-live="polite" className="text-sm tabular-nums">第 {pageNumber} / {pdf.numPages} 页</p>
        <button type="button" aria-label="PDF 下一页" disabled={pageNumber >= pdf.numPages}
          onClick={() => setPageNumber(previous => previous + 1)}
          className="rounded-lg border border-slate-200 p-2 disabled:opacity-30"><ChevronRight size={18} /></button>
      </div>
      <PdfCanvas key={pageNumber} pdf={pdf} pageNumber={pageNumber} />
    </div>
  )
}
