/** 审批回执只保存请求 ID 和指纹；恢复只读 Journal，绝不重新提交审批。 */
import { apiRequest } from './api'
import { parseTerminalResponse } from './agentStream'
import type { ChatResponse } from '../types/api'

export interface ReviewReceipt {
  request_id: string
  expected_fingerprints: Record<string, string>
}

const receipts = new Map<string, ReviewReceipt | null>()
const storageKey = (thread: string) => `sweet:unconfirmed-review:${thread}`

function validReceipt(value: unknown): value is ReviewReceipt {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const data = value as Record<string, unknown>
  return typeof data.request_id === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$/.test(data.request_id) &&
    !!data.expected_fingerprints && typeof data.expected_fingerprints === 'object' && !Array.isArray(data.expected_fingerprints) &&
    Object.entries(data.expected_fingerprints).length > 0 && Object.entries(data.expected_fingerprints).every(
      ([id, hash]) => id.length > 0 && typeof hash === 'string' && /^[0-9a-f]{64}$/.test(hash))
}

export function rememberReview(thread: string, receipt: ReviewReceipt) {
  if (!validReceipt(receipt)) throw new Error('审批回执无效')
  receipts.set(thread, receipt)
  try { sessionStorage.setItem(storageKey(thread), JSON.stringify(receipt)) } catch { /* 内存保护仍然生效。 */ }
}

export function getReviewReceipt(thread: string): ReviewReceipt | null {
  if (receipts.has(thread)) return receipts.get(thread) ?? null
  try {
    const raw = sessionStorage.getItem(storageKey(thread))
    const value: unknown = raw ? JSON.parse(raw) : null
    return validReceipt(value) ? value : null
  } catch {
    return receipts.get(thread) ?? null
  }
}

export function forgetReview(thread: string) {
  receipts.set(thread, null)
  try { sessionStorage.removeItem(storageKey(thread)) } catch { /* 无可用的持久化存储。 */ }
}

export async function recoverReview(thread: string, receipt: ReviewReceipt): Promise<ChatResponse | null> {
  const raw = await apiRequest<Record<string, unknown>>(`/api/v1/requests/${encodeURIComponent(receipt.request_id)}`)
  if (raw.request_id !== receipt.request_id || raw.thread_id !== thread || raw.operation !== 'review') {
    throw new Error('审批请求记录不匹配')
  }
  if ((raw.journal_status !== 'completed' && raw.journal_status !== 'pending_approval') || raw.has_saved_response !== true) {
    return null // Checkpoint 空闲不能证明这一条审批请求完成。
  }
  const result = parseTerminalResponse({ event: raw.journal_status, data: raw.response }, thread)
  for (const item of result.pending_approvals) {
    if (!item || typeof item !== 'object' || typeof (item as Record<string, unknown>).tool_call_id !== 'string') {
      throw new Error('恢复的审批列表无效')
    }
    if (Object.hasOwn(receipt.expected_fingerprints, (item as { tool_call_id: string }).tool_call_id)) {
      throw new Error('原审批调用仍在待审批列表中，不能重复批准')
    }
  }
  return result
}

export function reviewOutcomeNotice(result: ChatResponse): string {
  if (result.status === 'pending_approval') return '审批已确认，Agent 再次请求工具审批，请核对新的操作。'
  const outer = result.citation_report
  const report = outer?.mode === 'document_analysis' ? outer : outer?.document_analysis
  if (report?.analysis_status && report.analysis_status !== 'completed') {
    return '审批已确认；' + (report.analysis_error_message ??
      (report.analysis_status === 'partial' ? '集合分析仅部分完成，请查看报告中的失败和未确定项。' : '集合分析未执行或未完成，请查看回复中的原因。'))
  }
  return '审批已确认，本轮请求已结束。'
}
