
/**
 * 文件：src/services/chat.ts
 *
 * 用途：
 *   调用 Sweet Research Agent 的真实聊天 API。
 *
 * 主要功能：
 *   1. 向 FastAPI 发送用户消息。
 *   2. 指定 LangGraph thread_id。
 *   3. 支持 request_id 幂等保护。
 *   4. 返回 Agent 执行结果和状态。
 *
 * 安全设计：
 *   1. 不在这里自动重试请求。
 *   2. 不自动重复执行可能产生副作用的操作。
 *   3. 审批中断交给后续 HITL 组件处理。
 */

import { apiRequest } from './api'

import type {
  ChatRequest,
  ChatResponse,
} from '../types/api'

// 前端真实聊天要求先创建 Thread。
// 后端虽然允许省略 thread_id，
// 但这里显式绑定会话，便于幂等处理。
export type SendChatRequest = Omit<
  ChatRequest,
  'thread_id'
> & {
  thread_id: string
}

/**
 * 发送一条消息。
 *
 * 返回状态可能包括：
 * - completed：Agent 已完成
 * - pending_approval：等待用户审批
 *
 * 对于其他状态，不应直接判定为成功。
 */
export async function sendChat(
  payload: SendChatRequest,
): Promise<ChatResponse> {
  return apiRequest<ChatResponse>(
    '/api/v1/chat',
    {
      method: 'POST',

      headers: {
        'Content-Type': 'application/json',
      },

      body: JSON.stringify(payload),
    },
  )
}
