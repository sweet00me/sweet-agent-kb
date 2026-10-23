
/**
 * 文件：src/components/layout/Sidebar.tsx
 *
 * 用途：
 *   管理 Sweet Research Agent 的侧边栏。
 *
 * 主要功能：
 *   1. PC 端显示固定侧边栏。
 *   2. 手机端显示历史聊天抽屉。
 *   3. 切换历史会话。
 *   4. 新建聊天。
 *   5. PC 端页面导航。
 *
 * 设计说明：
 *   侧边栏不直接修改聊天数据。
 *   所有操作通过 Props 回调交给 App.tsx。
 */

import { useEffect, useId, useRef, useState } from 'react'

import {
  SquarePen,
  X,
  MessageSquare,
  LibraryBig,
  Activity,
  Pencil,
  Trash2,
  MoreHorizontal,
} from 'lucide-react'

import { ThreadActionDialog, type ThreadAction } from '../chat/ThreadActionDialog'
import type { Conversation } from '../../types/chat'
import type { PageTab } from '../../types/navigation'

interface SidebarProps {
  chats: Conversation[]
  activeId: string | null
  activeTab: PageTab
  drawerOpen: boolean
  actionsDisabled: boolean

  onSelectChat: (id: string) => void
  onNewChat: () => void
  onChangeTab: (tab: PageTab) => void
  onCloseDrawer: () => void
  onRenameChat: (id: string, title: string) => Promise<void>
  onDeleteChat: (id: string) => Promise<void>
}

// PC 端侧边栏导航配置
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

function HistoryMenu({
  chat,
  disabled,
  onManageChat,
}: {
  chat: Conversation
  disabled: boolean
  onManageChat: (action: ThreadAction, chat: Conversation) => void
}) {
  const menuId = useId()
  const menuRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState({ top: 0, left: 0 })

  useEffect(() => {
    if (!open) return
    const close = () => menuRef.current?.hidePopover()
    window.addEventListener('resize', close)
    window.addEventListener('scroll', close, true)
    return () => {
      window.removeEventListener('resize', close)
      window.removeEventListener('scroll', close, true)
    }
  }, [open])

  function choose(action: ThreadAction) {
    menuRef.current?.hidePopover()
    triggerRef.current?.focus()
    onManageChat(action, chat)
  }

  return (
    <div className="shrink-0 pr-2">
      <button
        ref={triggerRef}
        type="button"
        popoverTarget={menuId}
        aria-label={`会话操作：${chat.title}`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        title="会话操作"
        disabled={disabled}
        onClick={(event) => {
          const rect = event.currentTarget.getBoundingClientRect()
          setPosition({
            top: rect.bottom + 104 <= window.innerHeight
              ? rect.bottom + 4
              : Math.max(8, rect.top - 96),
            left: Math.max(8, Math.min(rect.right - 160, window.innerWidth - 168)),
          })
        }}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' && !open) {
            event.preventDefault()
            event.currentTarget.click()
          }
        }}
        className="rounded-lg p-2 text-slate-500 hover:bg-slate-200 hover:text-slate-900 disabled:opacity-40"
      >
        <MoreHorizontal size={18} />
      </button>

      <div
        id={menuId}
        ref={menuRef}
        popover="auto"
        role="menu"
        aria-label={`会话操作：${chat.title}`}
        style={{ ...position, right: 'auto', bottom: 'auto' }}
        onToggle={(event) => {
          const expanded = event.newState === 'open'
          setOpen(expanded)
          if (expanded) event.currentTarget.querySelector<HTMLButtonElement>('button')?.focus()
        }}
        onKeyDown={(event) => {
          if (event.key === 'Tab') {
            menuRef.current?.hidePopover()
            return
          }
          if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
          event.preventDefault()
          const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button'))
          const current = items.indexOf(document.activeElement as HTMLButtonElement)
          const next = event.key === 'Home' ? 0
            : event.key === 'End' ? items.length - 1
              : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
          items[next]?.focus()
        }}
        className="fixed m-0 w-40 rounded-xl border border-slate-200 bg-white p-1 text-sm font-normal text-slate-700 shadow-lg"
      >
        <button
          type="button"
          role="menuitem"
          disabled={disabled}
          onClick={() => choose('rename')}
          className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left hover:bg-slate-100 focus:bg-slate-100 focus:outline-none disabled:opacity-40"
        >
          <Pencil size={16} />
          重命名
        </button>
        <button
          type="button"
          role="menuitem"
          disabled={disabled}
          onClick={() => choose('delete')}
          className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-red-600 hover:bg-red-50 focus:bg-red-50 focus:outline-none disabled:opacity-40"
        >
          <Trash2 size={16} />
          删除
        </button>
      </div>
    </div>
  )
}

// 历史聊天列表：PC 与手机共用
function HistoryList({
  chats,
  activeId,
  activeTab,
  onSelectChat,
  actionsDisabled,
  onManageChat,
}: Pick<
  SidebarProps,
  'chats' | 'activeId' | 'activeTab' | 'onSelectChat' | 'actionsDisabled'
> & { onManageChat: (action: ThreadAction, chat: Conversation) => void }) {
  if (chats.length === 0) {
    return (
      <p className="px-3 py-4 text-sm text-slate-400">
        暂无历史聊天
      </p>
    )
  }

  return (
    <div className="space-y-1">
      {chats.map((chat) => {
        const isActive =
          activeId === chat.id && activeTab === 'chat'

        return (
          <div
            key={chat.id}
            className={`flex items-center rounded-xl text-sm transition-colors ${
              isActive
                ? 'bg-slate-200 font-medium'
                : 'hover:bg-slate-100'
            }`}
          >
            <button
              type="button"
              onClick={() => onSelectChat(chat.id)}
              className="min-w-0 flex-1 rounded-xl px-3 py-3 text-left"
              aria-current={isActive ? 'page' : undefined}
              title={chat.title}
            >
              <span className="block truncate">{chat.title}</span>
            </button>
            <HistoryMenu chat={chat} disabled={actionsDisabled} onManageChat={onManageChat} />
          </div>
        )
      })}
    </div>
  )
}

export function Sidebar({
  chats,
  activeId,
  activeTab,
  drawerOpen,
  onSelectChat,
  onNewChat,
  onChangeTab,
  onCloseDrawer,
  actionsDisabled,
  onRenameChat,
  onDeleteChat,
}: SidebarProps) {
  const [action, setAction] = useState<{ kind: ThreadAction; chat: Conversation } | null>(null)
  const historyProps = {
    chats,
    activeId,
    activeTab,
    onSelectChat,
    actionsDisabled,
    onManageChat: (kind: ThreadAction, chat: Conversation) => setAction({ kind, chat }),
  }

  return (
    <>
      {/* =====================================
          1. PC 端固定侧边栏
      ===================================== */}

      <aside className="hidden w-72 shrink-0 flex-col border-r border-slate-200 bg-slate-50 p-4 lg:flex">

        <div className="mb-5 flex items-center justify-between">
          <span className="font-bold">
            Sweet Research
          </span>

          <button
            type="button"
            onClick={onNewChat}
            aria-label="新建聊天"
            className="rounded-lg p-2 hover:bg-slate-200"
          >
            <SquarePen size={20} />
          </button>
        </div>

        {/* PC 页面导航 */}
        <nav
          aria-label="桌面端主导航"
          className="mb-5 flex flex-col gap-1"
        >
          {navigationItems.map(({ id, label, Icon }) => {
            const isActive = activeTab === id

            return (
              <button
                key={id}
                type="button"
                onClick={() => onChangeTab(id)}
                aria-current={isActive ? 'page' : undefined}
                className={`flex items-center gap-3 rounded-xl px-3 py-3 text-left text-sm ${
                  isActive
                    ? 'bg-slate-200 font-semibold text-slate-900'
                    : 'text-slate-500 hover:bg-slate-100'
                }`}
              >
                <Icon size={20} />
                {label}
              </button>
            )
          })}
        </nav>

        <div className="mb-2 text-xs text-slate-400">
          历史聊天
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          <HistoryList {...historyProps} />
        </div>
      </aside>

      {/* =====================================
          2. 手机端历史聊天抽屉
      ===================================== */}

      {drawerOpen && (
        <div className="fixed inset-0 z-50 lg:hidden">

          {/* 半透明遮罩 */}
          <button
            type="button"
            onClick={onCloseDrawer}
            aria-label="关闭历史聊天"
            className="absolute inset-0 bg-black/30"
          />

          {/* 抽屉主体 */}
          <aside
            aria-label="历史聊天"
            className="relative flex h-full w-[min(85vw,320px)] flex-col bg-white p-4 shadow-xl"
          >
            <div className="mb-5 flex items-center justify-between">
              <span className="font-semibold">
                历史聊天
              </span>

              <button
                type="button"
                onClick={onCloseDrawer}
                aria-label="关闭"
                className="rounded-lg p-2 hover:bg-slate-100"
              >
                <X size={22} />
              </button>
            </div>

            <button
              type="button"
              onClick={onNewChat}
              className="mb-5 flex items-center gap-2 rounded-xl bg-slate-100 px-3 py-3 text-sm"
            >
              <SquarePen size={18} />
              新建聊天
            </button>

            <div className="min-h-0 flex-1 overflow-y-auto">
              <HistoryList {...historyProps} />
            </div>
          </aside>
        </div>
      )}

      {action && (
        <ThreadActionDialog
          action={action.kind}
          chat={action.chat}
          onSubmit={(title) => action.kind === 'rename'
            ? onRenameChat(action.chat.id, title)
            : onDeleteChat(action.chat.id)}
          onClose={() => setAction(null)}
        />
      )}
    </>
  )
}
