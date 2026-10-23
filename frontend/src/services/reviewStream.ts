
/**
 * 文件：src/services/reviewStream.ts
 *
 * 用途：
 *   调用 LangGraph HITL 流式审批接口。
 *
 * 对应后端：
 *   POST /api/v1/threads/{thread_id}/review/stream
 *
 * 安全说明：
 *   只有用户完成批准/拒绝的二次确认后，
 *   App.tsx 才允许调用此服务。
 *
 *   本服务不自动生成审批决定，
 *   也不自动重试。
 */

import {
  runAgentStream,
  type AgentStreamCallback,
} from './agentStream'

import type {
  ChatResponse,
  ReviewRequest,
} from '../types/api'


export async function reviewThreadStream(
  threadId: string,
  payload: ReviewRequest,
  onEvent: AgentStreamCallback,
): Promise<ChatResponse> {
  const id = encodeURIComponent(threadId)

  return runAgentStream(
    `/api/v1/threads/${id}/review/stream`,
    payload,
    threadId,
    onEvent,
  )
}
