
/**
 * 文件：src/types/stream.ts
 *
 * 用途：
 *   定义 Sweet Research Agent SSE 通信的数据类型。
 *
 * 设计说明：
 *   SSE 事件的名称由后端决定。
 *   事件内容在完成运行时校验前保持 unknown，
 *   防止前端误信任网络数据。
 */

export interface SseEvent {
  event: string
  data: unknown
  id?: string
}

export interface PostSseOptions {
  path: string
  payload: unknown

  // 每收到一个完整 SSE 事件就调用一次。
  onEvent: (event: SseEvent) => void

  // 可选：控制前端网络连接。
  signal?: AbortSignal
}

/**
 * 从 token 事件中安全读取文本。
 */
export function getTokenText(
  event: SseEvent,
): string | null {
  if (
    event.event !== 'token' ||
    !event.data ||
    typeof event.data !== 'object'
  ) {
    return null
  }

  const data = event.data as Record<string, unknown>

  return typeof data.text === 'string'
    ? data.text
    : null
}

/**
 * 从 node 事件中安全读取节点名称。
 */
export function getNodeName(
  event: SseEvent,
): string | null {
  if (
    event.event !== 'node' ||
    !event.data ||
    typeof event.data !== 'object'
  ) {
    return null
  }

  const data = event.data as Record<string, unknown>

  return typeof data.name === 'string'
    ? data.name
    : null
}
