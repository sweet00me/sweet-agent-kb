
/**
 * 文件：src/components/chat/MarkdownRenderer.tsx
 *
 * 用途：
 *   将 Agent 返回的 Markdown 文本渲染成页面内容。
 *
 * 主要功能：
 *   1. Markdown 标题、列表和代码块渲染。
 *   2. 支持 GFM 表格等扩展语法。
 *   3. 适配移动端宽表格的横向滚动。
 *
 * 样式：
 *   复用 src/index.css 中的 markdown-content
 *   和 markdown-table-scroll 样式。
 */

import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface MarkdownRendererProps {
  content: string
}

export function MarkdownRenderer({
  content,
}: MarkdownRendererProps) {
  return (
    <div className="markdown-content min-w-0">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // 让表格独立横向滚动，避免撑破手机屏幕
          table: ({ children }) => (
            <div className="markdown-table-scroll">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}
