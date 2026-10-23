
/**
 * 文件：src/services/agentStream.ts
 *
 * 用途：
 *   统一处理 Sweet Research Agent SSE 执行结果。
 *
 * 主要功能：
 *   1. 调用底层 SSE 通信服务。
 *   2. 验证 completed / pending_approval 终止事件。
 *   3. 检查返回的 thread_id。
 *   4. 区分执行成功、审批暂停和执行异常。
 *
 * 安全设计：
 *   不能仅根据 HTTP 流正常关闭判断 Agent 成功。
 *   必须收到合法的终止事件。
 *   不自动重新提交任何请求。
 */

import { postSse } from './sse'

import type { AnalysisMetadata, ChatResponse, CitationReport } from '../types/api'
import type { SseEvent } from '../types/stream'


export type AgentStreamCallback = (
  event: SseEvent,
) => void


// =====================================
// 1. 校验最终响应
// =====================================

function parseCitationReport(raw: unknown): CitationReport | null {
  if (raw === null) return null
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('引用核验报告格式无效')
  }
  const value = raw as Record<string, unknown>
  if (value.mode === 'document_analysis') return parseAnalysisReport(value)
  if (value.mode !== undefined && !['plain', 'rag', 'rag_and_document_analysis'].includes(value.mode as string)) {
    throw new Error('引用核验报告类型无效')
  }
  const { status, reason, claim_count: count, citations, checked_at: checkedAt } = value
  if (
    (status !== 'verified' && status !== 'rejected' && status !== 'not_applicable') ||
    typeof reason !== 'string' || !reason ||
    typeof count !== 'number' || !Number.isSafeInteger(count) || count < 0 ||
    !Array.isArray(citations) ||
    (checkedAt !== undefined && typeof checkedAt !== 'string') ||
    (status === 'verified' && (count === 0 || citations.length === 0)) ||
    (value.mode === 'plain' && (status === 'verified' || count !== 0 || citations.length !== 0)) ||
    (value.mode === 'rag' && status === 'not_applicable')
  ) {
    throw new Error('引用核验报告内容无效')
  }
  const parsed = citations.map(rawCitation => {
    if (!rawCitation || typeof rawCitation !== 'object' || Array.isArray(rawCitation)) {
      throw new Error('引用来源格式无效')
    }
    const citation = rawCitation as Record<string, unknown>
    if (
      typeof citation.source !== 'string' || !citation.source.trim() ||
      typeof citation.chunk_id !== 'string' || !/^[0-9a-f]{32}$/.test(citation.chunk_id) ||
      typeof citation.file_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(citation.file_sha256) ||
      typeof citation.chunk_index !== 'number' || !Number.isSafeInteger(citation.chunk_index) || citation.chunk_index < 0 ||
      !Array.isArray(citation.quotes) || citation.quotes.length === 0 ||
      !citation.quotes.every(quote => typeof quote === 'string' && quote.trim())
    ) {
      throw new Error('引用来源或原文引文无效')
    }
    return {
      source: citation.source, chunk_id: citation.chunk_id,
      chunk_index: citation.chunk_index, file_sha256: citation.file_sha256,
      quotes: citation.quotes as string[],
    }
  })
  return {
    status, reason, claim_count: count, citations: parsed,
    ...((value.mode === 'plain' || value.mode === 'rag') ? { mode: value.mode } : {}),
    ...(typeof checkedAt === 'string' ? { checked_at: checkedAt } : {}),
    ...(value.mode === 'rag_and_document_analysis'
      ? { mode: value.mode, document_analysis: parseAnalysisReport(value.document_analysis) }
      : {}),
  }
}

function parseAnalysisReport(raw: unknown): CitationReport {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('集合分析报告格式无效')
  const value = raw as Record<string, unknown>
  const states = ['completed', 'partial', 'failed', 'blocked', 'cancelled', 'interrupted', 'version_changed', 'rejected', 'pending', 'running']
  if (value.mode !== 'document_analysis' || value.status !== 'not_applicable' ||
      typeof value.analysis_status !== 'string' || !states.includes(value.analysis_status) ||
      typeof value.analysis_complete !== 'boolean' || (value.analysis_complete && value.analysis_status !== 'completed') ||
      (value.analysis_id !== null && typeof value.analysis_id !== 'string') ||
      !Array.isArray(value.analysis_ids) || !value.analysis_ids.every(id => typeof id === 'string') ||
      !Array.isArray(value.sources) || value.real_world_truth !== 'not_checked' ||
      (value.reason !== undefined && (typeof value.reason !== 'string' || !value.reason)) ||
      (value.claim_count !== undefined && value.claim_count !== 0) ||
      (value.citations !== undefined && (!Array.isArray(value.citations) || value.citations.length !== 0))) {
    throw new Error('集合分析报告内容无效')
  }
  for (const field of ['analysis_error_code', 'analysis_error_message']) {
    if (value[field] !== undefined && value[field] !== null && typeof value[field] !== 'string') {
      throw new Error('集合分析错误原因无效')
    }
  }
  const sources = value.sources.map(rawSource => {
    if (!rawSource || typeof rawSource !== 'object' || Array.isArray(rawSource)) throw new Error('集合分析来源无效')
    const source = rawSource as Record<string, unknown>
    const location = source.location as Record<string, unknown> | undefined
    if (typeof source.evidence_id !== 'string' || !/^[0-9a-f]{64}$/.test(source.evidence_id) ||
        typeof source.source !== 'string' || !source.source.trim() ||
        typeof source.document_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(source.document_sha256) ||
        !location || typeof location !== 'object' || Array.isArray(location) ||
        ['start_char', 'end_char', 'start_line', 'end_line', 'start_column', 'end_column'].some(key =>
          typeof location[key] !== 'number' || !Number.isSafeInteger(location[key]) || (location[key] as number) < (key.endsWith('char') ? 0 : 1)) ||
        (location.end_char as number) <= (location.start_char as number)) {
      throw new Error('集合分析来源或原文位置无效')
    }
    return { evidence_id: source.evidence_id, source: source.source,
      document_sha256: source.document_sha256, location: location as Record<string, number> }
  })
  return {
    status: 'not_applicable', reason: (value.reason as string | undefined) ?? 'document_analysis_report',
    claim_count: 0, citations: [], mode: 'document_analysis',
    analysis_status: value.analysis_status as AnalysisMetadata['analysis_status'],
    analysis_complete: value.analysis_complete, analysis_id: value.analysis_id as string | null,
    analysis_ids: value.analysis_ids as string[], sources, real_world_truth: 'not_checked',
    ...(value.analysis_error_code !== undefined ? { analysis_error_code: value.analysis_error_code as string | null } : {}),
    ...(value.analysis_error_message !== undefined ? { analysis_error_message: value.analysis_error_message as string | null } : {}),
  }
}

export function parseTerminalResponse(
  event: SseEvent,
  expectedThreadId: string,
): ChatResponse {
  const raw = event.data

  if (!raw || typeof raw !== 'object') {
    throw new Error('Agent 终止事件格式无效')
  }

  const data = raw as Record<string, unknown>

  if (data.thread_id !== expectedThreadId) {
    throw new Error('SSE 返回了不匹配的 Thread ID')
  }

  if (data.status !== event.event) {
    throw new Error('SSE 事件与工作流状态不一致')
  }

  if (!Array.isArray(data.pending_approvals)) {
    throw new Error('SSE 审批列表格式无效')
  }

// 先校验来自网络的原始数据
const rawAnswer = data.answer

if (
  typeof rawAnswer !== 'string' &&
  rawAnswer !== null
) {
  throw new Error('Agent 回答格式无效')
}

// 显式收窄为 ChatResponse 要求的类型
const answer: string | null =
  typeof rawAnswer === 'string'
    ? rawAnswer
    : null

// completed 必须包含字符串回答
if (
  event.event === 'completed' &&
  answer === null
) {
  throw new Error('已完成的 Agent 请求缺少回答')
}

  return {
    thread_id: expectedThreadId,
    status: event.event,
    answer,
    pending_approvals: data.pending_approvals,
    ...(data.citation_report !== undefined
      ? { citation_report: parseCitationReport(data.citation_report) }
      : {}),
  }
}


// =====================================
// 2. 统一运行流式请求
// =====================================

export async function runAgentStream(
  path: string,
  payload: unknown,
  expectedThreadId: string,
  onEvent: AgentStreamCallback,
): Promise<ChatResponse> {
  // 使用对象保存回调结果，避免闭包变量
  // 在异步函数中出现类型收窄问题。
  const state: {
    result: ChatResponse | null
    error: string | null
  } = {
    result: null,
    error: null,
  }

  await postSse({
    path,
    payload,

    onEvent(event) {
      // 校验 started 事件所属的会话。
      if (event.event === 'started') {
        const data = event.data

        if (
          !data ||
          typeof data !== 'object' ||
          (data as Record<string, unknown>).thread_id
            !== expectedThreadId
        ) {
          throw new Error(
            'SSE started 事件的 Thread ID 不匹配',
          )
        }
        const expectedRequest = payload && typeof payload === 'object'
          ? (payload as Record<string, unknown>).request_id : undefined
        if (typeof expectedRequest === 'string' && (
          (data as Record<string, unknown>).request_id !== expectedRequest ||
          (data as Record<string, unknown>).status !== 'running'
        )) {
          throw new Error('SSE started 事件的请求 ID 或接收状态不匹配')
        }
      }

      // 后端异常事件，不允许视为完成。
      if (event.event === 'error') {
        const data = event.data

        const message =
          data &&
          typeof data === 'object' &&
          typeof (
            data as Record<string, unknown>
          ).message === 'string'
            ? (
                data as Record<string, string>
              ).message
            : 'Agent 执行状态无法确认'

        state.error = message
      }

      // 终止事件必须和后端结构一致。
      if (
        event.event === 'completed' ||
        event.event === 'pending_approval'
      ) {
        if (state.result !== null) {
          throw new Error('SSE 收到重复终止事件')
        }

        state.result = parseTerminalResponse(
          event,
          expectedThreadId,
        )
      }

      // 将事件转发给 React 更新界面。
      onEvent(event)
    },
  })

  if (state.error !== null) {
    throw new Error(state.error)
  }

  if (state.result === null) {
    throw new Error(
      'SSE 已断开，但没有收到 completed 或 pending_approval。执行结果尚未确认，请勿重新提交。',
    )
  }

  return state.result
}
