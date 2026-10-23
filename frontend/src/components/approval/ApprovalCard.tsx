
/**
 * 文件：src/components/approval/ApprovalCard.tsx
 *
 * 用途：
 *   展示一条等待人工审批的工具调用。
 *
 * 功能：
 *   1. 显示工具名称和调用 ID。
 *   2. 完整展示待执行参数。
 *   3. 允许选择批准或拒绝。
 *
 * 说明：
 *   这里只记录用户选择，不执行工具。
 */

import { Check, X, ShieldAlert } from 'lucide-react'

import type { PendingApproval } from '../../types/approval'
import type { ApprovalDecision } from '../../types/api'

interface ApprovalCardProps {
  item: PendingApproval
  decision?: ApprovalDecision
  disabled?: boolean
  onChange: (decision: ApprovalDecision) => void
}

export function ApprovalCard({
  item,
  decision,
  disabled = false,
  onChange,
}: ApprovalCardProps) {
  const argumentText =
    JSON.stringify(item.arguments, null, 2) ?? 'null'

  return (
    <article className="min-w-0 rounded-2xl border border-slate-200 bg-white p-4">
      <div className="mb-3 flex items-center gap-2">
        <ShieldAlert size={20} />
        <h3 className="font-semibold">工具审批</h3>
      </div>

      <p className="text-xs text-slate-500">
        工具名称
      </p>

      <p className="mt-1 break-all font-mono text-sm font-semibold">
        {item.tool_name}
      </p>

      <p className="mt-3 text-xs text-slate-500">
        工具调用 ID
      </p>

      <p className="mt-1 break-all font-mono text-xs">
        {item.tool_call_id}
      </p>

      <p className="mt-4 text-sm font-medium">
        待执行参数
      </p>

      {/* 参数完整展示，避免批准未阅读的内容 */}
      <pre className="mt-2 max-h-64 max-w-full overflow-auto rounded-xl bg-slate-100 p-3 text-xs leading-6">
        <code>{argumentText}</code>
      </pre>

      <p className="mt-2 text-xs text-slate-500">
        已附带后端 fingerprint 校验
      </p>

      <div className="mt-4 grid grid-cols-2 gap-3">
        <button
          type="button"
          disabled={disabled}
          aria-pressed={decision === 'reject'}
          onClick={() => onChange('reject')}
          className={`flex min-h-11 items-center justify-center gap-2 rounded-xl border text-sm font-medium disabled:opacity-50 ${
            decision === 'reject'
              ? 'border-rose-600 bg-rose-50 text-rose-700'
              : 'border-slate-300 text-slate-700'
          }`}
        >
          <X size={17} />
          拒绝
        </button>

        <button
          type="button"
          disabled={disabled}
          aria-pressed={decision === 'approve'}
          onClick={() => onChange('approve')}
          className={`flex min-h-11 items-center justify-center gap-2 rounded-xl border text-sm font-medium disabled:opacity-50 ${
            decision === 'approve'
              ? 'border-emerald-600 bg-emerald-50 text-emerald-700'
              : 'border-slate-300 text-slate-700'
          }`}
        >
          <Check size={17} />
          批准
        </button>
      </div>
    </article>
  )
}
