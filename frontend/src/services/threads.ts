
/**
 * 文件：src/services/threads.ts
 *
 * 用途：
 *   封装 Sweet Research Agent 的 Thread API。
 *
 * 功能：
 *   1. 创建新的聊天会话。
 *   2. 获取历史聊天列表。
 *   3. 获取指定会话的信息。
 *   4. 分页获取会话消息。
 *
 * 后端：
 *   FastAPI /api/v1/threads
 *
 * 设计：
 *   本文件不直接操作 React 状态。
 *   不管理 UI、不执行 LangGraph。
 *   只负责 HTTP API 通信。
 */

import { apiRequest } from './api'

import type {
  ThreadRecord,
  ThreadListResponse,
  MessageHistoryResponse,
  CreateThreadRequest,
  RenameThreadRequest,
  ThreadStatusResponse
} from '../types/api'

// =====================================
// 1. 创建聊天会话
// =====================================

export async function createThread(
  title = '新会话',
): Promise<ThreadRecord> {

  const payload: CreateThreadRequest = {
    title,
  }

  return apiRequest<ThreadRecord>(
    '/api/v1/threads',
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    },
  )
}

// =====================================
// 2. 获取历史聊天列表
// =====================================

export async function getThreads(
  limit = 50,
): Promise<ThreadListResponse> {

  const params = new URLSearchParams({
    limit: String(limit),
  })

  return apiRequest<ThreadListResponse>(
    `/api/v1/threads?${params.toString()}`,
  )
}

// =====================================
// 3. 获取单个会话信息
// =====================================

export async function getThread(
  threadId: string,
): Promise<ThreadRecord> {

  const id = encodeURIComponent(threadId)

  return apiRequest<ThreadRecord>(
    `/api/v1/threads/${id}`,
  )
}

/** 重命名会话，返回后端确认的新标题。 */
export async function renameThread(
  threadId: string,
  title: string,
): Promise<ThreadRecord> {
  const payload: RenameThreadRequest = { title }
  return apiRequest<ThreadRecord>(
    `/api/v1/threads/${encodeURIComponent(threadId)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  )
}

/** 软删除会话；后端成功返回 204，无响应正文。 */
export async function deleteThread(threadId: string): Promise<void> {
  await apiRequest<void>(
    `/api/v1/threads/${encodeURIComponent(threadId)}`,
    { method: 'DELETE' },
  )
}

// =====================================
// 4. 分页获取聊天消息
// =====================================

export async function getThreadMessages(
  threadId: string,
  limit = 100,
  beforeId?: number,
): Promise<MessageHistoryResponse> {

  const id = encodeURIComponent(threadId)

  const params = new URLSearchParams({
    limit: String(limit),
  })

  // 只有需要加载更早消息时，才带上游标
  if (beforeId !== undefined) {
    params.set(
      'before_id',
      String(beforeId),
    )
  }

  return apiRequest<MessageHistoryResponse>(
    `/api/v1/threads/${id}/messages?${params.toString()}`,
  )
}


/**
 * 查询 LangGraph 会话执行状态。
 *
 * 用于判断会话是否已经完成，
 * 或者是否存在待审批、未完成的工作流。
 */
export async function getThreadStatus(
  threadId: string,
): Promise<ThreadStatusResponse> {
  const id = encodeURIComponent(threadId)

  return apiRequest<ThreadStatusResponse>(
    `/api/v1/threads/${id}/status`,
  )
}
