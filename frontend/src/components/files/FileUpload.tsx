
/**
 * 文件：src/components/files/FileUpload.tsx
 *
 * 用途：
 *   Sweet Research Agent 文件上传页面。
 *
 * 功能：
 *   1. 从 iPhone 或 PC 选择本地文件。
 *   2. 查看文件名称和大小。
 *   3. 向 FastAPI 提交 multipart/form-data。
 *   4. 显示上传成功与失败状态。
 *   5. 防止连续点击造成重复上传。
 *
 * 安全说明：
 *   后端固定上传到 Storage/PC_Files/Uploads。
 *   不提供任意目标目录选择。
 *   网络异常后不自动重新上传。
 */

import {
  useRef,
  useState,
  type ChangeEvent,
} from 'react'

import {
  ArrowLeft,
  FileUp,
  FileText,
  Upload,
  CheckCircle2,
  AlertCircle,
  Folder,
  LoaderCircle,
} from 'lucide-react'

import { uploadFile } from '../../services/files'
import { ApiError } from '../../services/api'


interface FileUploadProps {
  onBack: () => void
}


// =====================================
// 1. 文件大小格式化
// =====================================

function formatSize(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`
  }

  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`
  }

  if (bytes < 1024 * 1024 * 1024) {
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  }

  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}


// =====================================
// 2. 后端上传错误说明
// =====================================

function describeUploadError(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.status) {
      case 400:
        return '文件参数不符合服务器要求，请检查文件名称、类型等限制。'

      case 409:
        return '目标目录中可能已有同名文件。服务器拒绝覆盖，请更换文件名称。'

      case 413:
        return '文件超过服务器允许的上传大小。'

      case 503:
        return '服务器存储服务暂时不可用，请稍后检查。'

      default:
        return error.message
    }
  }

  return '网络连接中断或未能确认上传结果。请先检查服务器 Uploads 目录，不要直接重复上传。'
}


// =====================================
// 3. 文件上传页面
// =====================================

export function FileUpload({
  onBack,
}: FileUploadProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const uploadingRef = useRef(false)

  const [selectedFile, setSelectedFile] =
    useState<File | null>(null)

  const [uploading, setUploading] = useState(false)
  const [successName, setSuccessName] = useState('')
  const [error, setError] = useState('')
  const [uncertain, setUncertain] = useState(false)


  // =====================================
  // 4. 选择本地文件
  // =====================================

  function handleFileChange(
    event: ChangeEvent<HTMLInputElement>,
  ) {
    if (uploadingRef.current) return

    const file = event.target.files?.[0] ?? null

    setSelectedFile(file)
    setSuccessName('')
    setError('')
    // 如果上一请求结果不确定，不会因重新选文件
    // 而自动解除上传保护。
  }


  // =====================================
  // 5. 执行上传
  // =====================================

  async function handleUpload() {
    if (
      !selectedFile ||
      uploadingRef.current ||
      uncertain
    ) {
      return
    }

    const file = selectedFile

    uploadingRef.current = true

    setUploading(true)
    setError('')
    setSuccessName('')

    try {
      // 服务端返回 HTTP 201 才表示上传成功。
      await uploadFile(file)

      setSuccessName(file.name)
      setSelectedFile(null)

      if (inputRef.current) {
        inputRef.current.value = ''
      }
    } catch (err) {
      console.error('文件上传失败:', err)

      setError(describeUploadError(err))

      // 没有可靠 HTTP 响应或服务器内部异常时，
      // 上传可能已经执行，不能立即自动重试。
      if (
        !(err instanceof ApiError) ||
        err.status >= 500
      ) {
        setUncertain(true)
      }
    } finally {
      uploadingRef.current = false
      setUploading(false)
    }
  }


  // =====================================
  // 6. 页面 UI
  // =====================================

  return (
    <div className="min-w-0 space-y-5">

      {/* 页面标题 */}
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={onBack}
          disabled={uploading}
          aria-label="返回知识库首页"
          className="rounded-xl border border-slate-200 p-2 disabled:opacity-50"
        >
          <ArrowLeft size={21} />
        </button>

        <div>
          <h2 className="text-lg font-semibold">
            文件上传
          </h2>

          <p className="mt-1 text-xs text-slate-500">
            上传本地科研资料到 sweetserver
          </p>
        </div>
      </div>


      {/* 文件选择区域 */}
      <div className="rounded-2xl border border-dashed border-slate-300 bg-slate-50 p-6 text-center">

        <div className="mb-3 flex justify-center">
          <div className="rounded-2xl bg-white p-4">
            <FileUp
              size={32}
              className="text-slate-600"
            />
          </div>
        </div>

        <p className="text-sm font-semibold">
          选择需要上传的文件
        </p>

        <p className="mt-2 text-xs leading-6 text-slate-500">
          从 iPhone 文件 App 或 PC 中选择文件。
          实际允许类型和大小由服务器校验。
        </p>

        <input
          ref={inputRef}
          id="research-file-upload"
          type="file"
          disabled={uploading || uncertain}
          onChange={handleFileChange}
          className="sr-only"
        />

        <label
          htmlFor="research-file-upload"
          className={`mt-4 inline-flex min-h-11 items-center justify-center rounded-xl border border-slate-300 bg-white px-5 text-sm font-medium ${
            uploading || uncertain
              ? 'pointer-events-none opacity-50'
              : 'cursor-pointer hover:bg-slate-100'
          }`}
        >
          <FileText size={17} className="mr-2" />
          选择文件
        </label>
      </div>


      {/* 已选文件 */}
      {selectedFile && (
        <div className="rounded-xl border border-slate-200 bg-white p-4">
          <p className="mb-2 text-xs text-slate-500">
            当前选中的文件
          </p>

          <p className="break-all text-sm font-medium">
            {selectedFile.name}
          </p>

          <p className="mt-2 text-xs text-slate-500">
            文件大小：{formatSize(selectedFile.size)}
          </p>
        </div>
      )}


      {/* 固定目标目录 */}
      <div className="rounded-xl bg-slate-100 p-4">
        <p className="mb-2 text-xs text-slate-500">
          固定上传位置
        </p>

        <div className="flex items-start gap-2">
          <Folder
            size={19}
            className="mt-0.5 shrink-0 text-slate-600"
          />

          <p className="break-all text-sm font-medium leading-6">
            Storage / PC_Files / Uploads
          </p>
        </div>

        <p className="mt-3 text-xs leading-6 text-slate-500">
          当前服务器不支持自定义上传目录。
          同名文件不会被覆盖。
        </p>
      </div>


      {/* 上传按钮 */}
      <button
        type="button"
        disabled={
          !selectedFile ||
          uploading ||
          uncertain
        }
        onClick={() => void handleUpload()}
        className="flex min-h-12 w-full items-center justify-center gap-2 rounded-xl bg-slate-900 px-4 py-3 text-sm font-medium text-white disabled:opacity-40"
      >
        {uploading ? (
          <>
            <LoaderCircle
              size={19}
              className="animate-spin"
            />
            正在上传...
          </>
        ) : (
          <>
            <Upload size={19} />
            开始上传
          </>
        )}
      </button>


      {/* 上传中提示 */}
      {uploading && (
        <p
          role="status"
          className="text-center text-xs leading-6 text-slate-500"
        >
          正在向服务器上传文件，请保持当前页面打开。
        </p>
      )}


      {/* 上传成功 */}
      {successName && (
        <div
          role="status"
          className="flex gap-3 rounded-xl border border-emerald-200 bg-emerald-50 p-4"
        >
          <CheckCircle2
            size={21}
            className="shrink-0 text-emerald-700"
          />

          <div className="min-w-0">
            <p className="text-sm font-semibold text-emerald-900">
              文件上传成功
            </p>

            <p className="mt-1 break-all text-xs leading-6 text-emerald-800">
              {successName}
            </p>

            <p className="mt-2 text-xs text-emerald-800">
              服务器已确认上传完成，可前往
              Storage / PC_Files / Uploads 查看。
            </p>
          </div>
        </div>
      )}


      {/* 上传失败 */}
      {error && (
        <div
          role="alert"
          className="flex gap-3 rounded-xl border border-rose-200 bg-rose-50 p-4"
        >
          <AlertCircle
            size={21}
            className="shrink-0 text-rose-700"
          />

          <p className="break-words text-sm leading-6 text-rose-800">
            {error}
          </p>
        </div>
      )}

    </div>
  )
}
