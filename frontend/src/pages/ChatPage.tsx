
/**
 * 文件：src/pages/ChatPage.tsx
 *
 * 用途：
 *   Sweet Research Agent 聊天主页面。
 *
 * 功能：
 *   1. 展示真实聊天消息。
 *   2. 支持 Markdown 渲染。
 *   3. 自动滚动到最新内容。
 *   4. 展示待审批工具。
 *   5. 接收用户审批决定。
 *
 * 说明：
 *   实际 HTTP 请求由 App.tsx 统一管理。
 */

import {
  useEffect,
  useRef,
  type FormEvent,
} from 'react'

import { Bot } from 'lucide-react'

import { ChatInput } from '../components/chat/ChatInput'
import { MessageList } from '../components/chat/MessageList'
import { ApprovalList } from '../components/approval/ApprovalList'

import type { Conversation } from '../types/chat'
import type { PendingApproval } from '../types/approval'
import type { ApprovalDecision } from '../types/api'
import { StreamStatus } from '../components/chat/StreamStatus'

interface ChatPageProps {
  conversation?: Conversation
  draft: string
  onDraftChange: (value: string) => void
  onSendMessage: (event: FormEvent<HTMLFormElement>) => void

  isBusy?: boolean
  submissionStatus?: 'sending' | 'accepted' | null
  canSend?: boolean
  streamActive?: boolean
  streamNode?: string | null
  streamText?: string
  pendingUserText?: string | null
  pendingApprovals?: PendingApproval[]
  reviewBlocked?: boolean

  onReview?: (
    decisions: Record<string, ApprovalDecision>,
  ) => Promise<void>
}

export function ChatPage({
  conversation,
  draft,
  onDraftChange,
  onSendMessage,
  isBusy = false,
  submissionStatus = null,
  canSend = true,
  pendingApprovals = [],
  reviewBlocked = false,
  onReview,
  streamActive = false,
  streamNode = null,
  streamText = '',
  pendingUserText = null,
}: ChatPageProps) {
  const chatScrollRef = useRef<HTMLDivElement>(null)

  const conversationId = conversation?.id
  const messageCount = conversation?.messages.length ?? 0

  // 审批集合变化时，重新创建审批组件，
  // 避免把上一组工具的选择带入新审批。
  const approvalKey = pendingApprovals
    .map((item) => `${item.tool_call_id}:${item.fingerprint}`)
    .join('|')

  useEffect(() => {
    const container = chatScrollRef.current

    if (!container) return

    container.scrollTop = container.scrollHeight
  }, [
    conversationId,
    messageCount,
    approvalKey,
    streamActive,
    streamText,
  ])

  return (
    <section className="flex min-h-0 flex-1 flex-col">

      <div
        ref={chatScrollRef}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-5"
      >
        <div className="mx-auto flex min-h-full w-full max-w-3xl flex-col">

          {!conversation?.messages.length &&
            pendingApprovals.length === 0 &&
            !streamActive &&
            pendingUserText === null ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-4 text-center">
              <div className="rounded-2xl bg-slate-100 p-4">
                <Bot size={34} />
              </div>

              <h1 className="text-2xl font-semibold">
                今天想研究什么？
              </h1>

              <p className="text-sm text-slate-500">
                你的私人科研知识库与 AI Agent
              </p>
            </div>
          ) : (
            <>
              {conversation &&
                conversation.messages.length > 0 && (
                  <MessageList
                    messages={conversation.messages}
                  />
                )}

              {pendingUserText !== null && (
                <div className="mt-4 flex justify-end">
                  <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl bg-slate-100 px-4 py-3 text-sm leading-7">
                    {pendingUserText}
                  </div>
                </div>
              )}

              <StreamStatus
                active={streamActive}
                node={streamNode}
                text={streamText}
              />

              {pendingApprovals.length > 0 && onReview && (
                <ApprovalList
                  key={approvalKey}
                  items={pendingApprovals}
                  busy={isBusy}
                  blocked={reviewBlocked}
                  onSubmit={onReview}
                />
              )}
            </>
          )}

        </div>
      </div>

      <ChatInput
        value={draft}
        onChange={onDraftChange}
        onSubmit={onSendMessage}
        disabled={isBusy || !canSend}
        statusText={
          isBusy
            ? submissionStatus === 'sending' ? '正在发送消息...'
              : submissionStatus === 'accepted' ? '消息已接收，Agent 正在处理...'
                : 'Agent 正在处理请求...'
            : pendingApprovals.length > 0
              ? '等待工具审批'
              : canSend
                ? '已连接 Research Agent'
                : '当前会话暂不可发送'
        }
      />

    </section>
  )
}
