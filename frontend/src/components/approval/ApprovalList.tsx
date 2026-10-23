
/**
 * 文件：src/components/approval/ApprovalList.tsx
 *
 * 用途：
 *   管理当前会话中的全部待审批工具。
 *
 * 主要功能：
 *   1. 为每个工具选择批准或拒绝。
 *   2. 检查是否所有工具都有决定。
 *   3. 提交前二次确认。
 *   4. 提交期间禁止重复操作。
 *
 * 说明：
 *   实际 HTTP 请求由 App.tsx 负责。
 */

import { useState } from 'react'
import { ApprovalCard } from './ApprovalCard'

import type { PendingApproval } from '../../types/approval'
import type { ApprovalDecision } from '../../types/api'

interface ApprovalListProps {
  items: PendingApproval[]
  busy: boolean
  blocked?: boolean
  onSubmit: (
    decisions: Record<string, ApprovalDecision>,
  ) => Promise<void>
}

export function ApprovalList({
  items,
  busy,
  blocked = false,
  onSubmit,
}: ApprovalListProps) {
  const [decisions, setDecisions] = useState<
    Record<string, ApprovalDecision>
  >({})

  const [confirming, setConfirming] = useState(false)

  const allSelected =
    items.length > 0 &&
    items.every((item) => Boolean(
      decisions[item.tool_call_id],
    ))

  const approveCount = items.filter(
    (item) => decisions[item.tool_call_id] === 'approve',
  ).length

  const rejectCount = items.filter(
    (item) => decisions[item.tool_call_id] === 'reject',
  ).length

  function choose(
    id: string,
    decision: ApprovalDecision,
  ) {
    setDecisions((previous) => ({
      ...previous,
      [id]: decision,
    }))

    // 修改任何决定后，必须重新确认
    setConfirming(false)
  }

  const disabled = busy || blocked

  return (
    <section
      aria-label="待审批工具"
      className="mt-5 min-w-0 space-y-3"
    >
      <h2 className="text-base font-semibold">
        等待工具审批（{items.length}）
      </h2>

      {items.map((item) => (
        <ApprovalCard
          key={item.tool_call_id}
          item={item}
          decision={decisions[item.tool_call_id]}
          disabled={disabled}
          onChange={(decision) =>
            choose(item.tool_call_id, decision)
          }
        />
      ))}

      {blocked ? (
        <p
          role="alert"
          className="rounded-xl bg-amber-50 p-3 text-sm text-amber-900"
        >
          上一次审批结果尚未确认。
          为避免重复操作，当前禁止再次提交审批。
        </p>
      ) : confirming ? (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-4">
          <p className="text-sm font-semibold">
            请再次确认审批决定
          </p>

          <p className="mt-2 text-sm text-slate-700">
            批准 {approveCount} 项，拒绝 {rejectCount} 项。
            批准的工具可能产生实际操作。
          </p>

          <div className="mt-4 grid grid-cols-2 gap-2">
            <button
              type="button"
              disabled={disabled}
              onClick={() => setConfirming(false)}
              className="rounded-xl border border-slate-300 px-3 py-3 text-sm"
            >
              返回修改
            </button>

            <button
              type="button"
              disabled={disabled || !allSelected}
              onClick={() => {
                void onSubmit(decisions)
              }}
              className="rounded-xl bg-slate-900 px-3 py-3 text-sm text-white disabled:opacity-50"
            >
              确认并提交
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          disabled={disabled || !allSelected}
          onClick={() => setConfirming(true)}
          className="min-h-12 w-full rounded-xl bg-slate-900 px-4 py-3 text-sm font-medium text-white disabled:opacity-40"
        >
          {busy ? '正在处理审批...' : '确认审批决定'}
        </button>
      )}

      {!allSelected && !blocked && (
        <p className="text-xs text-slate-500">
          请先为所有待审批工具选择批准或拒绝。
        </p>
      )}
    </section>
  )
}
