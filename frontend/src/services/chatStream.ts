
/**
 * 文件：src/services/chatStream.ts
 *
 * 用途：
 *   调用 Agent 流式聊天接口。
 *
 * 对应后端：
 *   POST /api/v1/chat/stream
 *
 * 说明：
 *   保留原来的非流式 sendChat()。
 *   本服务不自动重试请求。
 */

import {
  runAgentStream,
  type AgentStreamCallback,
} from './agentStream'

import type { SendChatRequest } from './chat'
import type { ChatResponse } from '../types/api'


export async function sendChatStream(
  payload: SendChatRequest,
  onEvent: AgentStreamCallback,
): Promise<ChatResponse> {
  return runAgentStream(
    '/api/v1/chat/stream',
    payload,
    payload.thread_id,
    onEvent,
  )
}
