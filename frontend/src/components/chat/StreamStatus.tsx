
/**
 * 文件：src/components/chat/StreamStatus.tsx
 *
 * 用途：
 *   在手机和 PC 端显示 Agent 实时运行进度。
 *
 * 主要功能：
 *   1. 显示当前 LangGraph 执行节点。
 *   2. 展示模型正在生成的临时文本。
 *   3. 提供清晰的处理中状态。
 *
 * 说明：
 *   临时文字不写入聊天历史。
 *   正式消息以 SQLite 持久化结果为准。
 */

import { Bot, LoaderCircle } from 'lucide-react'


interface StreamStatusProps {
  active: boolean
  node: string | null
  text: string
}

const NODE_LABELS: Record<string, string> = {
  agent: 'Agent 正在思考和生成回答',
  approval: '正在检查工具审批要求',
  controlled_tools: '正在执行已授权工具',
  summarize: '正在整理会话上下文',
}


export function StreamStatus({
  active,
  node,
  text,
}: StreamStatusProps) {
  if (!active) return null

  const description = node
    ? NODE_LABELS[node] ?? `执行节点：${node}`
    : '正在连接 Agent...'

  return (
    <section
      aria-label="Agent 实时执行状态"
      className="mt-4 min-w-0 rounded-2xl border border-slate-200 bg-white p-4"
    >
      <div className="flex items-center gap-3">
        <div className="rounded-xl bg-slate-100 p-2">
          <Bot size={19} />
        </div>

        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold">
            Sweet Research Agent
          </p>

          <p
            role="status"
            className="mt-1 text-xs text-slate-500"
          >
            {description}
          </p>
        </div>

        <LoaderCircle
          size={18}
          className="animate-spin text-slate-500"
        />
      </div>

      {text && (
        <div className="mt-4 border-t border-slate-100 pt-4">
          <div className="whitespace-pre-wrap break-words text-sm leading-7 text-slate-800">
            {text}
          </div>
        </div>
      )}
    </section>
  )
}
