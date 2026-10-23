
/**
 * 文件：src/components/chat/ChatInput.tsx
 *
 * 用途：
 *   Sweet Research Agent 的聊天消息输入组件。
 *
 * 主要功能：
 *   1. 输入多行文本。
 *   2. 发送消息。
 *   3. 禁止发送空消息。
 *   4. 适配手机和 PC。
 *
 * 设计说明：
 *   只负责输入界面。
 *   消息状态和发送逻辑由父组件管理。
 *   后续可扩展文件上传、发送状态和停止生成。
 */

import { useRef, type FormEvent } from 'react'
import { ArrowUp } from 'lucide-react'

interface ChatInputProps {
  value: string
  onChange: (value: string) => void
  onSubmit: (event: FormEvent<HTMLFormElement>) => void
  disabled?: boolean
  statusText?: string
}

export function ChatInput({
  value,
  onChange,
  onSubmit,
  disabled = false,
  statusText = '已连接 Research Agent',
}: ChatInputProps) {
  const composing = useRef(false)
  return (
    <form
      onSubmit={(event) => {
        if (composing.current) { event.preventDefault(); return }
        onSubmit(event)
      }}
      className="shrink-0 px-3 pb-3 pt-2"
    >
      <div className="mx-auto max-w-3xl rounded-2xl border border-slate-200 bg-white p-3 shadow-sm">
        <textarea
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onCompositionStart={() => { composing.current = true }}
          onCompositionEnd={() => { composing.current = false }}
          onKeyDown={(event) => {
            if (event.key !== 'Enter' || event.shiftKey) return
            if (composing.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
            event.preventDefault()
            if (!disabled && !event.repeat && value.trim()) event.currentTarget.form?.requestSubmit()
          }}
          disabled={disabled}
          maxLength={10000}
          placeholder="向 Research Agent 发送消息..."
          rows={2}
          aria-label="聊天消息"
          className="w-full resize-none bg-transparent text-base outline-none placeholder:text-slate-400"
        />

        <div className="flex items-center justify-between">
          <span className="text-xs text-slate-400">
            {statusText}
          </span>

          <button
            type="submit"
            disabled={disabled || !value.trim()}
            aria-label="发送消息"
            className="rounded-full bg-slate-900 p-2 text-white disabled:opacity-30"
          >
            <ArrowUp size={19} />
          </button>
        </div>
      </div>
    </form>
  )
}
