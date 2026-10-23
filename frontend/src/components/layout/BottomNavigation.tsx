
/**
 * 文件：src/components/layout/BottomNavigation.tsx
 *
 * 用途：
 *   Sweet Research Agent 移动端底部导航。
 *
 * 功能：
 *   1. 聊天、知识库、记录页面切换。
 *   2. 高亮当前页面。
 *   3. 适配 iPhone 底部安全区域。
 *
 * 设计：
 *   PC 端隐藏，由侧边栏承担导航功能。
 */

import {
  MessageSquare,
  LibraryBig,
  Activity,
} from 'lucide-react'

import type { PageTab } from '../../types/navigation'

interface BottomNavigationProps {
  activeTab: PageTab
  onChange: (tab: PageTab) => void
}

const navigationItems = [
  {
    id: 'chat',
    label: '聊天',
    Icon: MessageSquare,
  },
  {
    id: 'knowledge',
    label: '知识库',
    Icon: LibraryBig,
  },
  {
    id: 'records',
    label: '记录',
    Icon: Activity,
  },
] as const

export function BottomNavigation({
  activeTab,
  onChange,
}: BottomNavigationProps) {
  return (
    <nav
      aria-label="主导航"
      className="safe-bottom grid shrink-0 grid-cols-3 border-t border-slate-100 bg-white lg:hidden"
    >
      {navigationItems.map(({ id, label, Icon }) => {
        const isActive = activeTab === id

        return (
          <button
            key={id}
            type="button"
            onClick={() => onChange(id)}
            aria-current={isActive ? 'page' : undefined}
            className={`flex min-h-14 flex-col items-center justify-center gap-1 transition-colors ${
              isActive
                ? 'text-slate-900'
                : 'text-slate-400'
            }`}
          >
            <Icon size={21} />

            <span className="text-xs">
              {label}
            </span>
          </button>
        )
      })}
    </nav>
  )
}
