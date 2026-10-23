
/**
 * 文件：src/components/chat/MessageList.tsx
 *
 * 用途：
 *   展示用户与 Agent 的聊天消息。
 *
 * 设计：
 *   1. 用户消息右对齐，使用聊天气泡。
 *   2. Agent 回答使用整个可用宽度。
 *   3. Markdown 表格、代码块保留横向滚动。
 *   4. 优先适配移动端屏幕。
 */

import type { Message } from '../../types/chat'
import { MarkdownRenderer } from './MarkdownRenderer'

interface MessageListProps {
  messages: Message[]
}

export function MessageList({
  messages,
}: MessageListProps) {
  return (
    <div className="w-full min-w-0 space-y-5">
      {messages.map((message) => {
        const isUser = message.role === 'user'

        return (
          <div
            key={message.id}
            className={`flex min-w-0 ${
              isUser ? 'justify-end' : 'justify-start'
            }`}
          >
            <div
              className={
                isUser
                  ? 'min-w-0 max-w-[85%] rounded-2xl bg-slate-100 px-4 py-3 text-sm leading-7'
                  : 'w-full min-w-0 max-w-full py-2 text-sm leading-7'
              }
            >
              {isUser ? (
                <div className="whitespace-pre-wrap break-words">
                  {message.content}
                </div>
              ) : (
                <MarkdownRenderer content={message.content} />
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
