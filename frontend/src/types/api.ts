
/**
 * 文件：src/types/api.ts
 *
 * 用途：
 *   定义 Sweet Research Agent FastAPI
 *   请求与响应的数据类型。
 *
 * 数据来源：
 *   app/api/main.py
 *   app/api/session_store.py
 *
 * 设计原则：
 *   1. 与后端实际数据结构保持一致。
 *   2. API 类型与前端 UI 类型分离。
 *   3. 不擅自假设尚未确认的字段。
 */

// =====================================
// 1. 创建会话
// =====================================

export interface CreateThreadRequest {
  title: string
}

export interface RenameThreadRequest {
  title: string
}

// 会话基本信息
export interface ThreadRecord {
  thread_id: string
  title: string
  created_at: string
  updated_at: string
}

// =====================================
// 2. 历史会话列表
// =====================================

// 历史列表包含消息数量
export interface ThreadSummary extends ThreadRecord {
  message_count: number
}

export interface ThreadListResponse {
  items: ThreadSummary[]
}

// =====================================
// 3. 聊天消息历史
// =====================================

export interface StoredMessage {
  id: number
  thread_id: string
  role: 'user' | 'assistant'
  content: string
  created_at: string
}

export interface MessageHistoryResponse {
  items: StoredMessage[]

  // null 表示已经没有更早的消息
  next_before_id: number | null
}

// =====================================
// 4. Agent 聊天接口
// =====================================

export interface ChatRequest {
  message: string
  thread_id?: string | null
  request_id?: string
}

export interface AnalysisMetadata {
  analysis_status: 'completed' | 'partial' | 'failed' | 'blocked' | 'cancelled' | 'interrupted' | 'version_changed' | 'rejected' | 'pending' | 'running'
  analysis_complete: boolean
  analysis_id: string | null
  analysis_ids: string[]
  analysis_error_code?: string | null
  analysis_error_message?: string | null
  real_world_truth: 'not_checked'
  sources: Array<{
    evidence_id: string
    source: string
    document_sha256: string
    location: Record<string, number>
  }>
}

export interface CitationReport extends Partial<AnalysisMetadata> {
  status: 'verified' | 'rejected' | 'not_applicable'
  reason: string
  claim_count: number
  checked_at?: string
  mode?: 'plain' | 'rag' | 'document_analysis' | 'rag_and_document_analysis'
  document_analysis?: CitationReport
  citations: Array<{
    source: string
    chunk_id: string
    chunk_index: number
    file_sha256: string
    quotes: string[]
  }>
}

export interface ChatResponse {
  thread_id: string
  status: string
  answer: string | null

  // 审批对象的具体结构后续确认
  pending_approvals: unknown[]
  citation_report?: CitationReport | null
}

/**
 * 会话执行状态。
 *
 * 对应 GET /api/v1/threads/{thread_id}/status。
 */
export interface ThreadStatusResponse {
  thread_id: string
  status: string
  message_count: number
  pending_approvals: unknown[]
}


/**
 * 工具审批相关 API 类型。
 *
 * 对应：
 * POST /api/v1/threads/{thread_id}/review
 *
 * decisions：
 *   工具调用标识 -> approve/reject
 *
 * expected_fingerprints：
 *   工具调用标识 -> 后端提供的 fingerprint
 */

export type ApprovalDecision = 'approve' | 'reject'

export interface ReviewRequest {
  decisions: Record<string, ApprovalDecision>

  expected_fingerprints: Record<string, string>

  request_id?: string | null
}
