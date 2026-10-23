
/**
 * 文件：src/services/sse.ts
 *
 * 用途：
 *   提供通用的 HTTP POST + SSE 流式通信能力。
 *
 * 主要功能：
 *   1. 使用 fetch 发起 POST 请求。
 *   2. 通过 ReadableStream 持续读取响应。
 *   3. 正确处理跨网络数据块的 SSE 事件。
 *   4. 解析 event / data / id 字段。
 *   5. 忽略 SSE 心跳注释。
 *
 * 安全说明：
 *   1. 不自动重试请求。
 *   2. 网络断开不代表服务端停止执行。
 *   3. Promise 正常结束不代表 Agent completed。
 *   4. 最终状态由调用方结合后端事件和 Thread API 判断。
 */

import type {
  PostSseOptions,
  SseEvent,
} from '../types/stream'
import { ApiError } from './api'


// =====================================
// 1. 解析单条 SSE 事件
// =====================================

function parseSseFrame(
  frame: string,
): SseEvent | null {
  let eventName = 'message'
  let eventId: string | undefined

  const dataLines: string[] = []

  for (const line of frame.split('\n')) {
    if (!line || line.startsWith(':')) {
      // 空行和心跳注释不属于事件内容。
      continue
    }

    const colon = line.indexOf(':')

    const field =
      colon === -1
        ? line
        : line.slice(0, colon)

    let value =
      colon === -1
        ? ''
        : line.slice(colon + 1)

    // SSE 规范允许字段值开头有一个空格。
    if (value.startsWith(' ')) {
      value = value.slice(1)
    }

    switch (field) {
      case 'event':
        eventName = value
        break

      case 'data':
        dataLines.push(value)
        break

      case 'id':
        eventId = value
        break

      default:
        // 例如 retry，当前客户端无需处理。
        break
    }
  }

  // 纯心跳或没有 data 的内容不作为事件。
  if (dataLines.length === 0) {
    return null
  }

  const rawData = dataLines.join('\n')

  // 当前后端 encode_sse() 始终发送 JSON。
  // 如果 JSON 无效，停止流式处理，而不是猜测数据。
  const data: unknown = JSON.parse(rawData)

  return {
    event: eventName,
    data,
    ...(eventId !== undefined
      ? { id: eventId }
      : {}),
  }
}


// =====================================
// 2. 发送 POST SSE 请求
// =====================================

export async function postSse({
  path,
  payload,
  onEvent,
  signal,
}: PostSseOptions): Promise<void> {
  const response = await fetch(path, {
    method: 'POST',

    headers: {
      Accept: 'text/event-stream',
      'Content-Type': 'application/json',
    },

    body: JSON.stringify(payload),
    signal,
  })

  // 普通 HTTP 错误可能返回 JSON，而不是 SSE。
  if (!response.ok) {
    const detail = await response.text()

    throw new ApiError(response.status, `SSE 请求失败 (${response.status}): ${detail.slice(0, 500)}`)
  }

  // 防止把代理服务器返回的 HTML 页面误当作 SSE。
  const contentType =
    response.headers.get('content-type') ?? ''

  if (!contentType.includes('text/event-stream')) {
    throw new Error(
      `响应不是 SSE：${contentType || '未知类型'}`,
    )
  }

  if (!response.body) {
    throw new Error('服务器未返回可读取的数据流')
  }

  const reader = response.body.getReader()

  // 使用增量解码，避免 UTF-8 中文字符被拆坏。
  const decoder = new TextDecoder('utf-8')

  let buffer = ''
  let reachedEnd = false

  try {
    while (true) {
      const { done, value } = await reader.read()

      if (done) {
        reachedEnd = true
        break
      }

      buffer += decoder.decode(value, {
        stream: true,
      })

      // 兼容 CRLF 和 LF 两种换行形式。
      buffer = buffer.replace(/\r\n/g, '\n')

      let separatorIndex = buffer.indexOf('\n\n')

      // 一次数据块可能包含多条 SSE 事件。
      while (separatorIndex !== -1) {
        const frame = buffer.slice(
          0,
          separatorIndex,
        )

        buffer = buffer.slice(separatorIndex + 2)

        const parsed = parseSseFrame(frame)

        if (parsed) {
          onEvent(parsed)
        }

        separatorIndex = buffer.indexOf('\n\n')
      }

      // 防止服务端连续发送异常的超大未结束事件。
      if (buffer.length > 1024 * 1024) {
        throw new Error('SSE 单条事件超过缓冲区限制')
      }
    }

    // 处理 UTF-8 解码器中剩余的数据。
    buffer += decoder.decode()

    // 后端正常事件都以空行结束。
    // 如果留下半条事件，不能认为接收成功。
    if (buffer.trim()) {
      throw new Error('SSE 连接结束时存在不完整的事件')
    }
  } finally {
    if (!reachedEnd) {
      // 只释放客户端流连接。
      // 后端设计保证已经启动的 Agent 继续执行。
      try {
        await reader.cancel()
      } catch {
        // 网络本身已断开时无需再次抛错。
      }
    }

    reader.releaseLock()
  }
}
