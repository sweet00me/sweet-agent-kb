
/**
 * 文件：src/components/layout/Header.tsx
 *
 * 用途：
 *   Sweet Research Agent 顶部导航栏。
 *
 * 功能：
 *   1. 手机端打开历史聊天抽屉。
 *   2. 新建聊天。
 *   3. 打开设置。
 *
 * 设计：
 *   只负责界面与事件通知。
 *   不直接管理聊天状态。
 */

import {
  PanelLeft,
  SquarePen,
  Settings,
} from 'lucide-react'

interface HeaderProps {
  onOpenHistory: () => void
  onNewChat: () => void
  onOpenSettings: () => void
}

export function Header({
  onOpenHistory,
  onNewChat,
  onOpenSettings,
}: HeaderProps) {
  return (
    <header className="flex h-14 shrink-0 items-center justify-between border-b border-slate-100 bg-white px-4">
      <div className="flex min-w-0 items-center gap-3">
        {/* 历史聊天按钮只在移动端显示 */}
        <button
          type="button"
          onClick={onOpenHistory}
          className="rounded-lg p-1 lg:hidden"
          aria-label="打开历史聊天"
        >
          <PanelLeft size={23} />
        </button>

        <span className="truncate font-semibold">
          Sweet Research
        </span>
      </div>

      <div className="flex items-center gap-4">
        <button
          type="button"
          onClick={onNewChat}
          className="rounded-lg p-1"
          aria-label="新建聊天"
        >
          <SquarePen size={21} />
        </button>

        <button
          type="button"
          onClick={onOpenSettings}
          className="rounded-lg p-1"
          aria-label="设置"
        >
          <Settings size={21} />
        </button>
      </div>
    </header>
  )
}
