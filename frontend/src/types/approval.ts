
/**
 * 文件：src/types/approval.ts
 *
 * 用途：
 *   定义待审批工具的数据结构。
 *   校验 FastAPI 返回的审批数据。
 *
 * 安全原则：
 *   缺少工具 ID、名称或 fingerprint 时，
 *   不允许执行审批。
 */

export interface PendingApproval {
  tool_call_id: string
  tool_name: string
  arguments: unknown
  fingerprint: string
}

export function parsePendingApprovals(
  value: unknown,
): PendingApproval[] {
  if (!Array.isArray(value)) {
    throw new Error('待审批数据不是数组')
  }

  const ids = new Set<string>()

  return value.map((raw) => {
    if (!raw || typeof raw !== 'object') {
      throw new Error('待审批工具数据格式无效')
    }

    const item = raw as Record<string, unknown>

    if (
      typeof item.tool_call_id !== 'string' ||
      !item.tool_call_id ||
      typeof item.tool_name !== 'string' ||
      !item.tool_name ||
      typeof item.fingerprint !== 'string' ||
      !item.fingerprint ||
      !Object.prototype.hasOwnProperty.call(
        item, 'arguments',
      )
    ) {
      throw new Error('待审批工具缺少必要字段')
    }

    if (ids.has(item.tool_call_id)) {
      throw new Error('存在重复的工具调用 ID')
    }

    ids.add(item.tool_call_id)

    return {
      tool_call_id: item.tool_call_id,
      tool_name: item.tool_name,
      arguments: item.arguments,
      fingerprint: item.fingerprint,
    }
  })
}
