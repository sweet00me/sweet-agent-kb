import { useEffect, useId, useRef, useState, type FormEvent } from 'react'

import type { Conversation } from '../../types/chat'

export type ThreadAction = 'rename' | 'delete'

interface ThreadActionDialogProps {
  action: ThreadAction
  chat: Conversation
  onSubmit: (title: string) => Promise<void>
  onClose: () => void
}

export function ThreadActionDialog({
  action,
  chat,
  onSubmit,
  onClose,
}: ThreadActionDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const headingId = useId()
  const titleId = useId()
  const [title, setTitle] = useState(chat.title)
  const [submitting, setSubmitting] = useState(false)
  const submittingRef = useRef(false)
  const [error, setError] = useState('')
  const renaming = action === 'rename'

  useEffect(() => {
    const dialog = dialogRef.current
    dialog?.showModal()
    return () => dialog?.close()
  }, [])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (submittingRef.current || (renaming && !title.trim())) return

    submittingRef.current = true
    setSubmitting(true)
    setError('')
    try {
      await onSubmit(title.trim())
      onClose()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '操作失败，请稍后重试。')
    } finally {
      submittingRef.current = false
      setSubmitting(false)
    }
  }

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby={headingId}
      onCancel={(event) => {
        if (submittingRef.current) event.preventDefault()
        else onClose()
      }}
      className="m-auto w-[calc(100%-2rem)] max-w-sm rounded-2xl bg-white p-6 text-slate-900 shadow-xl backdrop:bg-black/40"
    >
      <form onSubmit={(event) => void submit(event)}>
        <h2 id={headingId} className="mb-4 text-lg font-semibold">
          {renaming ? '重命名会话' : '删除会话'}
        </h2>

        {renaming ? (
          <>
            <label htmlFor={titleId} className="mb-2 block text-sm text-slate-600">
              会话名称（最多 80 字）
            </label>
            <input
              id={titleId}
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              maxLength={80}
              required
              disabled={submitting}
              className="w-full rounded-xl border border-slate-300 px-3 py-2 text-base outline-none focus:border-slate-600 focus:ring-2 focus:ring-slate-200"
            />
          </>
        ) : (
          <div className="space-y-2 text-sm leading-6 text-slate-600">
            <p className="break-words">确定从历史列表删除“{chat.title}”？</p>
            <p>本地聊天记录和审计日志会保留。</p>
          </div>
        )}

        {error && <p role="alert" className="mt-3 break-words text-sm text-red-600">{error}</p>}

        <div className="mt-6 flex justify-end gap-3">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="rounded-xl border border-slate-200 px-4 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={submitting || (renaming && !title.trim())}
            className={`rounded-xl px-4 py-2 text-sm text-white disabled:opacity-50 ${
              renaming ? 'bg-slate-900 hover:bg-slate-700' : 'bg-red-600 hover:bg-red-700'
            }`}
          >
            {submitting ? '处理中…' : renaming ? '保存' : '确认删除'}
          </button>
        </div>
      </form>
    </dialog>
  )
}
