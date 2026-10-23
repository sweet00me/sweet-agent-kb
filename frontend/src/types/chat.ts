
/**
 * 文件：src/types/chat.ts
 *
 * 用途：
 *   定义 Sweet Research Agent 前端使用的聊天数据类型。
 *
 * 设计说明：
 *   当前类型用于模拟数据。
 *   后续对接 LangGraph Thread API 时，
 *   可以在此统一扩展消息和会话结构。
 */

// 消息角色
export type MessageRole = 'user' | 'assistant'

// 单条聊天消息
export interface Message {
  id: string
  role: MessageRole
  content: string
}

// 一个完整的聊天会话
export interface Conversation {
  id: string
  title: string
  messages: Message[]
}
