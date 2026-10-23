
/**
 * 文件：src/services/api.ts
 *
 * 用途：
 *   统一管理前端 HTTP 请求。
 *
 * 主要功能：
 *   1. 调用 FastAPI 后端。
 *   2. 解析 JSON 响应。
 *   3. 处理 HTTP 错误。
 *   4. 为 Chat、Thread、Knowledge API 提供基础能力。
 *
 * 设计说明：
 *   使用相对路径，通过 Vite Proxy 访问后端。
 *   不在前端存储模型 API Key。
 */


/**
 * 自定义 API 错误类型。
 *
 * 用途：
 *   保留 HTTP 状态码和错误信息，
 *   便于前端区分 404、422、500 等异常。
 *
 * 兼容：
 *   TypeScript erasableSyntaxOnly 模式。
 */
export class ApiError extends Error {
  // 显式声明类属性，避免使用参数属性语法
  public status: number

  constructor(status: number, message: string) {
    super(message)

    this.name = 'ApiError'
    this.status = status
  }
}


/**
 * 统一 HTTP 请求函数。
 *
 * T 是调用者预期的响应类型。
 * 注意：TypeScript 泛型并不执行运行时数据校验。
 */
export async function apiRequest<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {

  const headers = new Headers(options.headers)

  if (!headers.has('Accept')) {
    headers.set('Accept', 'application/json')
  }

  const response = await fetch(path, {
    ...options,
    headers,
  })

  const raw = await response.text()

  let data: unknown = null

  if (raw) {
    try {
      data = JSON.parse(raw)
    } catch {
      data = raw
    }
  }

  // 非 2xx 响应统一抛出错误
  if (!response.ok) {
    const detail =
      data !== null
        ? typeof data === 'string'
          ? data
          : JSON.stringify(data)
        : response.statusText

    throw new ApiError(
      response.status,
      `API 请求失败 (${response.status}): ${detail}`,
    )
  }

  return data as T
}
