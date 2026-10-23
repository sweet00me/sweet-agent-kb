
/**
 * 文件：src/App.tsx
 *
 * 用途：
 *   Sweet Research Agent Web UI 主应用。
 *
 * 主要功能：
 *   1. 从 FastAPI 加载真实聊天会话。
 *   2. 查询历史消息和 LangGraph 会话状态。
 *   3. 新建 Thread 并发送 Agent 消息。
 *   4. 使用 request_id 防止重复执行。
 *   5. 管理聊天、知识库、记录三个页面。
 *
 * 安全说明：
 *   对于执行结果不确定的请求，不自动重试。
 *   待审批线程暂时禁止继续发送新消息。
 */

import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from 'react'

import { X } from 'lucide-react'

import { Header } from './components/layout/Header'
import { Sidebar } from './components/layout/Sidebar'
import { BottomNavigation } from './components/layout/BottomNavigation'

import { ChatPage } from './pages/ChatPage'
import { KnowledgePage } from './pages/KnowledgePage'
import { RecordsPage } from './pages/RecordsPage'

import { createThread, getThreads, getThreadMessages, getThreadStatus, renameThread, deleteThread } from './services/threads'


import type { Message, Conversation } from './types/chat'
import type { PageTab } from './types/navigation'
import type { ChatResponse, StoredMessage, ThreadSummary } from './types/api'



import {
  parsePendingApprovals,
  type PendingApproval,
} from './types/approval'

import type { ApprovalDecision } from './types/api'

import { sendChatStream } from './services/chatStream'
import { apiRequest, ApiError } from './services/api'
import { reviewThreadStream } from './services/reviewStream'
import { rememberReview, getReviewReceipt, forgetReview, recoverReview, reviewOutcomeNotice } from './services/reviewRecovery'

import {
  getTokenText,
  getNodeName,
} from './types/stream'

import type { SseEvent } from './types/stream'


// =====================================
// 1. 数据转换工具
// =====================================

/**
 * 将后端数据库消息转换为 React UI 消息。
 *
 * 后端消息 ID 是 number，
 * 前端消息组件使用 string ID。
 */
function convertMessages(
  messages: StoredMessage[],
): Message[] {
  return messages.map((message) => ({
    id: String(message.id),
    role: message.role,
    content: message.content,
  }))
}

/**
 * 将 Thread 列表合并到已有前端状态。
 *
 * 已经加载的消息继续保留，
 * 不会因为刷新会话列表而丢失。
 */
function mergeThreads(
  items: ThreadSummary[],
  previous: Conversation[],
): Conversation[] {
  const listed = items.map((thread) => {
    const existing = previous.find(
      (chat) => chat.id === thread.thread_id,
    )

    return {
      id: thread.thread_id,
      title: thread.title,
      messages: existing?.messages ?? [],
    }
  })

  // 保留可能刚创建、尚未出现在列表响应中的会话。
  const additional = previous.filter(
    (chat) => !items.some(
      (thread) => thread.thread_id === chat.id,
    ),
  )

  return [...additional, ...listed]
}

/**
 * 为每次真实的用户发送操作生成请求 ID。
 *
 * 使用 getRandomValues，而不是 crypto.randomUUID：
 * 当前开发站点通过 Tailscale HTTP IP 访问，
 * randomUUID 在非安全上下文中可能不可用。
 */
function createRequestId(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)

  const hex = Array.from(bytes)
    .map((value) => value.toString(16).padStart(2, '0'))
    .join('')

  return `req_${hex}`
}


// =====================================
// 2. App 主组件
// =====================================

export default function App() {
  const [tab, setTab] = useState<PageTab>('chat')

  // 真实历史会话，不再初始化演示数据
  const [chats, setChats] = useState<Conversation[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)

  const [draft, setDraft] = useState('')
  const drafts = useRef(new Map<string | null, string>())
  const unconfirmedChats = useRef(new Map<string, { request_id: string; content: string }>())
  const [submissionStatus, setSubmissionStatus] = useState<'sending' | 'accepted' | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)

  const [isBusy, setIsBusy] = useState(false)
  const [isLoadingHistory, setIsLoadingHistory] = useState(false)
  const [canSend, setCanSend] = useState(true)
  const [notice, setNotice] = useState('')

  // 使用 Ref 同步阻止连续点击发送。
  const busyRef = useRef(false)

  // 防止快速切换聊天时，旧请求覆盖新页面。
  const selectionVersion = useRef(0)

  const activeChat = chats.find(
    (chat) => chat.id === activeId,
  )

  function changeDraft(value: string) {
    drafts.current.set(activeId, value)
    setDraft(value)
  }

  async function confirmUnconfirmedChat(threadId: string, version: number) {
    const receipt = unconfirmedChats.current.get(threadId)
    if (!receipt) return true
    const record = await apiRequest<Record<string, unknown>>(`/api/v1/requests/${encodeURIComponent(receipt.request_id)}`)
    if (version !== selectionVersion.current) return false
    if (record.request_id !== receipt.request_id || record.thread_id !== threadId || record.operation !== 'chat') {
      throw new Error('聊天请求记录不匹配')
    }
    if (!['completed', 'pending_approval'].includes(String(record.journal_status)) || record.has_saved_response !== true) {
      setNotice(`原请求执行结果尚未确认，草稿已保留，请勿重复发送。请求 ID：${receipt.request_id}。`)
      return false
    }
    // 只确认原请求已被后端处理；回答与审批仍由现有历史和状态接口恢复。
    if (drafts.current.get(threadId)?.trim() === receipt.content) drafts.current.delete(threadId)
    setDraft(previous => previous.trim() === receipt.content ? '' : previous)
    unconfirmedChats.current.delete(threadId)
    return true
  }

    // 当前会话中的待审批工具
  const [pendingApprovals, setPendingApprovals] =
    useState<PendingApproval[]>([])

  // 记录本次页面生命周期内结果不明确的审批会话。
  // 此类会话不允许再次提交审批请求。
  const [blockedReviewThreads, setBlockedReviewThreads] =
    useState<string[]>([])

  const reviewBlocked =
    activeId !== null &&
    blockedReviewThreads.includes(activeId)

    // 流式执行状态
  const [streamActive, setStreamActive] = useState(false)
  const [streamNode, setStreamNode] = useState<string | null>(null)
  const [streamText, setStreamText] = useState('')

  // 显示正在提交的用户消息，避免流式期间聊天区空白
  const [pendingUserText, setPendingUserText] =
    useState<string | null>(null)


  /**
   * 处理后端 SSE 增量事件。
   *
   * 最终消息仍然从 SQLite 获取。
   */
  function handleStreamEvent(event: SseEvent) {
    if (event.event === 'token') {
      const token = getTokenText(event)

      if (token !== null) {
        setStreamText((previous) => previous + token)
      }
    }

    if (event.event === 'node') {
      const node = getNodeName(event)

      if (node !== null) {
        setStreamNode(node)
      }
    }

    if (event.event === 'draft_reset') {
      // 模型转为调用工具，清除非最终文本
      setStreamText('')
    }
  }

  function clearReviewBlock(threadId: string) {
    forgetReview(threadId)
    setBlockedReviewThreads(previous => previous.filter(id => id !== threadId))
  }

  function applyConfirmedReview(threadId: string, result: ChatResponse, requestId: string) {
    const approvals = result.status === 'pending_approval' ? parsePendingApprovals(result.pending_approvals) : []
    const receipt = getReviewReceipt(threadId)
    if (receipt && approvals.some(item => Object.hasOwn(receipt.expected_fingerprints, item.tool_call_id))) {
      throw new Error('原审批调用仍在待审批列表中，不能重复批准')
    }
    clearReviewBlock(threadId)
    setPendingApprovals(approvals)
    setCanSend(result.status === 'completed')
    setNotice(reviewOutcomeNotice(result))
    // 终止响应已经持久化；历史接口暂时失败也不能撤销审批确认或丢失失败原因。
    if (result.answer !== null) {
      const message: Message = { id: `review-${requestId}`, role: 'assistant', content: result.answer }
      setChats(previous => previous.map(chat => chat.id === threadId
        ? { ...chat, messages: [...chat.messages.filter(item => item.id !== message.id), message] } : chat))
    }
  }


  // =====================================
  // 3. 首次加载历史会话
  // =====================================

  useEffect(() => {
    let cancelled = false

    getThreads()
      .then((response) => {
        if (cancelled) return

        setChats((previous) =>
          mergeThreads(response.items, previous),
        )
      })
      .catch((error: unknown) => {
        if (cancelled) return

        console.error('加载历史聊天失败:', error)

        setNotice(
          '历史聊天加载失败，请检查 FastAPI 连接。',
        )
      })

    return () => {
      cancelled = true
    }
  }, [])


  // =====================================
  // 4. 刷新会话列表
  // =====================================

  async function refreshThreads() {
    const response = await getThreads()

    setChats((previous) =>
      mergeThreads(response.items, previous),
    )
  }

  async function renameChat(threadId: string, title: string) {
    if (busyRef.current) throw new Error('当前请求仍在执行，请等待完成。')
    busyRef.current = true
    setIsBusy(true)
    try {
      const record = await renameThread(threadId, title)
      setChats((previous) => previous.map((chat) =>
        chat.id === threadId ? { ...chat, title: record.title } : chat,
      ))
    } finally {
      busyRef.current = false
      setIsBusy(false)
    }
  }

  async function deleteChat(threadId: string) {
    if (busyRef.current) throw new Error('当前请求仍在执行，请等待完成。')
    busyRef.current = true
    setIsBusy(true)
    try {
      await deleteThread(threadId)
      drafts.current.delete(threadId)
      unconfirmedChats.current.delete(threadId)
      forgetReview(threadId)
      setChats((previous) => previous.filter((chat) => chat.id !== threadId))
      setBlockedReviewThreads((previous) => previous.filter((id) => id !== threadId))
      if (activeId === threadId) {
        // 让旧的历史读取失效，删除当前会话后回到空白聊天。
        selectionVersion.current += 1
        setActiveId(null)
        setPendingApprovals([])
        setDraft('')
        setCanSend(true)
        setIsLoadingHistory(false)
        setNotice('')
        setTab('chat')
        setDrawerOpen(false)
      }
    } finally {
      busyRef.current = false
      setIsBusy(false)
    }
  }


  // =====================================
  // 5. 新建聊天
  // =====================================

  function newChat() {
    if (busyRef.current) {
      setNotice('当前请求仍在执行，请等待完成。')
      return
    }

    // 让之前可能尚未完成的历史读取失效。
    selectionVersion.current += 1
    setPendingApprovals([])
    setActiveId(null)
    setTab('chat')
    setDraft('')
    drafts.current.delete(null)
    setDrawerOpen(false)
    setIsLoadingHistory(false)
    setCanSend(true)
    setNotice('')

    // 不立即创建后端 Thread。
    // 等用户发送第一条消息时再创建。
  }


  // =====================================
  // 6. 打开真实历史聊天
  // =====================================


  /**
   * 打开历史聊天。
   *
   * 功能：
   *   1. 从 SQLite 加载真实历史消息。
   *   2. 查询 LangGraph Thread 当前状态。
   *   3. 恢复待审批工具信息。
   *   4. 控制当前会话是否允许发送新消息。
   *
   * 安全设计：
   *   对于 pending_approval 和 incomplete 状态，
   *   禁止发送新的聊天消息。
   */
  async function openChat(threadId: string) {
    if (busyRef.current) {
      setNotice('请等待当前 Agent 请求完成。')
      return
    }

    const version = ++selectionVersion.current

    // 清空上一条会话的审批内容
    setPendingApprovals([])

    setActiveId(threadId)
    setDraft(drafts.current.get(threadId) ?? '')
    setTab('chat')
    setDrawerOpen(false)
    setIsLoadingHistory(true)
    setCanSend(false)
    setNotice('')

    const receipt = getReviewReceipt(threadId)
    let recovered: ChatResponse | null = null

    try {
      if (!await confirmUnconfirmedChat(threadId, version)) return
      if (receipt) {
        setBlockedReviewThreads(previous => previous.includes(threadId) ? previous : [...previous, threadId])
        recovered = await recoverReview(threadId, receipt)
        if (version !== selectionVersion.current) return
        if (!recovered) {
          setNotice(`审批结果尚未确认，请检查状态，不要重复批准。请求 ID：${receipt.request_id}`)
          return
        }
        applyConfirmedReview(threadId, recovered, receipt.request_id)
      }
      // 先从后端读取历史消息与线程状态
      const [history, threadStatus] = await Promise.all([
        getThreadMessages(threadId),
        getThreadStatus(threadId),
      ])

      // 防止快速切换会话时旧请求覆盖新状态
      if (version !== selectionVersion.current) return

      // 更新历史消息
      setChats((previous) =>
        previous.map((chat) =>
          chat.id === threadId
            ? {
                ...chat,
                messages: convertMessages(history.items),
              }
            : chat,
        ),
      )

      // 现在才能读取 threadStatus
      const status = threadStatus.status

      // 从后端恢复真实的待审批工具
      const approvals =
        status === 'pending_approval'
          ? parsePendingApprovals(
              threadStatus.pending_approvals,
            )
          : []

      setPendingApprovals(approvals)

      // 根据 LangGraph 状态决定是否允许继续聊天
      if (status === 'completed' || status === 'empty') {
        clearReviewBlock(threadId)
        setCanSend(true)

        if (recovered) setNotice(reviewOutcomeNotice(recovered))

        if (history.next_before_id !== null) {
          setNotice(
            '当前显示最近 100 条消息，更早消息将在分页功能中支持。',
          )
        }
      } else if (status === 'pending_approval') {
        setCanSend(false)

        setNotice(
          approvals.length > 0
            ? 'Agent 正在等待工具审批，请检查下方的工具调用信息。'
            : 'Agent 正在等待审批，但没有获取到可操作的审批信息。',
        )
      } else {
        setCanSend(false)

        setNotice(
          `当前会话状态为 ${status}，暂时禁止继续发送。`,
        )
      }
    } catch (error) {
      if (version !== selectionVersion.current) return

      console.error('打开历史聊天失败:', error)

      if (recovered) {
        setNotice(reviewOutcomeNotice(recovered) + ' 历史消息暂时加载失败，可检查状态刷新。')
        return
      }

      setPendingApprovals([])
      setCanSend(false)

      setNotice(
        receipt ? `审批结果尚未确认，无法读取原请求状态，请勿重复批准。请求 ID：${receipt.request_id}`
          : '读取历史消息或会话状态失败，请稍后重新打开此会话。',
      )
    } finally {
      if (version === selectionVersion.current) {
        setIsLoadingHistory(false)
      }
    }
  }



/**
 * 手动检查当前会话状态。
 *
 * 作用：
 *   1. 从后端重新读取消息历史。
 *   2. 检查 LangGraph Thread 执行状态。
 *   3. 恢复可以安全继续的新消息输入。
 *
 * 注意：
 *   不重新提交原请求。
 *   不能代替 request_id 级别的精确核验。
 */
async function checkThreadState() {
  if (!activeId || busyRef.current) return

  const threadId = activeId
  const version = ++selectionVersion.current

  setIsLoadingHistory(true)
  setCanSend(false)
  setNotice('正在检查会话状态...')

  const receipt = getReviewReceipt(threadId)
  let recovered: ChatResponse | null = null

  try {
    if (!await confirmUnconfirmedChat(threadId, version)) return
    if (receipt) {
      recovered = await recoverReview(threadId, receipt)
      if (version !== selectionVersion.current) return
      if (!recovered) {
        setPendingApprovals([])
        setBlockedReviewThreads(previous => previous.includes(threadId) ? previous : [...previous, threadId])
        setNotice(`审批结果尚未确认，请勿重复批准。请求 ID：${receipt.request_id}`)
        return
      }
      applyConfirmedReview(threadId, recovered, receipt.request_id)
    }
    const [history, threadStatus] = await Promise.all([
      getThreadMessages(threadId),
      getThreadStatus(threadId),
    ])

    if (version !== selectionVersion.current) return

    // 更新数据库中的真实消息
    setChats((previous) =>
      previous.map((chat) =>
        chat.id === threadId
          ? {
              ...chat,
              messages: convertMessages(history.items),
            }
          : chat,
      ),
    )

    const status = threadStatus.status

    const approvals =
      threadStatus.status === 'pending_approval'
        ? parsePendingApprovals(
            threadStatus.pending_approvals,
          )
        : []

    setPendingApprovals(approvals)

    if (status === 'completed') {
      clearReviewBlock(threadId)
      setCanSend(true)

      setNotice(
        recovered ? reviewOutcomeNotice(recovered) : '当前会话已完成。消息已重新加载，请核对上一条请求的记录，不要重复发送原消息。',
      )
    } else if (status === 'pending_approval') {
      setCanSend(false)

      setNotice(
        'Agent 正在等待工具审批。请勿重复发送原消息。',
      )
    } else if (status === 'empty') {
      setCanSend(false)

      setNotice(
        '当前会话没有已记录的执行结果。如果此前发送失败，需要先核实请求记录，不要直接重试。',
      )
    } else {
      setCanSend(false)

      setNotice(
        `当前会话状态：${status}。工作流可能尚未结束，请勿重复提交。`,
      )
    }
  } catch (error) {
    if (version !== selectionVersion.current) return

    console.error('检查会话状态失败:', error)

    if (recovered) {
      setNotice(reviewOutcomeNotice(recovered) + ' 历史消息暂时加载失败，可稍后检查状态。')
      return
    }

    setCanSend(false)
    setNotice(
      receipt ? `审批结果尚未确认，无法读取原请求状态，请勿重复批准。请求 ID：${receipt.request_id}`
        : '无法确认当前会话状态，请检查网络或 FastAPI 服务。',
    )
  } finally {
    if (version === selectionVersion.current) {
      setIsLoadingHistory(false)
    }
  }
}


  // =====================================
  // 7. 真实 Agent 消息发送
  // =====================================

  async function submitChat(content: string) {
    if (busyRef.current) return

    busyRef.current = true
    setIsBusy(true)
    setNotice('')
    setStreamActive(true)
    setStreamNode(null)
    setStreamText('')
    setPendingUserText(content)
    setSubmissionStatus('sending')

    let threadId = activeId
    const requestId = createRequestId()
    let receivedResponse = false
    let submitted = false
    let accepted = false
    const acknowledge = () => {
      accepted = true
      if (threadId !== null) unconfirmedChats.current.delete(threadId)
      setSubmissionStatus('accepted')
      if (threadId !== null && drafts.current.get(threadId)?.trim() === content) drafts.current.delete(threadId)
      setDraft(previous => previous.trim() === content ? '' : previous)
    }

    try {
      // 第一次发送：创建真实会话
      if (threadId === null) {
        const created = await createThread(
          content.slice(0, 60),
        )

        threadId = created.thread_id
        const pendingDraft = drafts.current.get(null)
        if (pendingDraft !== undefined) {
          drafts.current.set(threadId, pendingDraft)
          drafts.current.delete(null)
        }

        setChats((previous) => [
          {
            id: created.thread_id,
            title: created.title,
            messages: [],
          },
          ...previous,
        ])

        setActiveId(threadId)
      }

      // 发送真实 Agent 请求。
      // 一个发送操作仅提交一次，不进行自动重试。
      submitted = true
      const result = await sendChatStream(
        {
          thread_id: threadId,
          message: content,
          request_id: requestId,
        },
        event => {
          // runAgentStream 已核对 thread_id/request_id/status；started 前消息已在后端持久化。
          if (event.event === 'started') acknowledge()
          handleStreamEvent(event)
        },
      )

      receivedResponse = true

      // 已收到后端明确响应，可以清空输入框。
      acknowledge()

      // 从 SQLite 重新读取已保存的消息。
      const history = await getThreadMessages(threadId)

      setChats((previous) =>
        previous.map((chat) =>
          chat.id === threadId
            ? {
                ...chat,
                messages: convertMessages(history.items),
              }
            : chat,
        ),
      )

      if (result.status === 'completed') {
        setCanSend(true)
        setNotice('')
      } else if (result.status === 'pending_approval') {
        setCanSend(false)
        const approvals = parsePendingApprovals(
          result.pending_approvals,
        )

        setPendingApprovals(approvals)

        setNotice(
          'Agent 已暂停，正在等待工具审批。请勿重新发送原消息。',
        )
      } else {
        setCanSend(false)

        setNotice(
          `Agent 当前状态：${result.status}。请检查会话状态。`,
        )
      }

      // 更新侧边栏的标题及最近活动排序。
      try {
        await refreshThreads()
      } catch (error) {
        console.error('刷新会话列表失败:', error)

        setNotice((previous) =>
          previous || '消息已处理，但历史会话列表刷新失败。',
        )
      }
    } catch (error) {
      console.error('Agent 请求失败:', error)

      // 网络失败时，无法证明服务端没有执行。
      // 禁止当前线程直接重复提交。
      const rejected = error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 409
      if (submitted && !accepted && !rejected && threadId !== null) {
        unconfirmedChats.current.set(threadId, { request_id: requestId, content })
      }
      setCanSend(!submitted || (!accepted && rejected))

      setNotice(
        receivedResponse
          ? 'Agent 已返回结果，但读取消息历史失败。请重新打开会话检查记录。'
          : accepted
            ? `消息已接收，执行结果尚未确认；请检查状态，不要重复发送。请求 ID：${requestId}。`
            : !submitted || rejected
              ? '消息尚未提交或未被接受，草稿已保留，请检查后再发送。'
              : `请求结果尚未确认，草稿已保留；请勿重复提交。请求 ID：${requestId}。请查看会话历史或审计记录。`,
      )
    } finally {
      busyRef.current = false
      setIsBusy(false)
      setStreamActive(false)
      setStreamNode(null)
      setStreamText('')
      setPendingUserText(null)
      setSubmissionStatus(null)
    }
  }


  /**
   * 提交真实 HITL 审批。
   *
   * 安全原则：
   *   1. 一次提交包含所有待审批工具的决定。
   *   2. fingerprint 来自当前后端状态。
   *   3. 使用唯一 request_id。
   *   4. 不自动重试审批。
   */
  async function submitReview(
    decisions: Record<string, ApprovalDecision>,
  ) {
    const threadId = activeId

    if (
      !threadId ||
      busyRef.current ||
      blockedReviewThreads.includes(threadId) ||
      getReviewReceipt(threadId) !== null ||
      pendingApprovals.length === 0
    ) {
      return
    }

    // 严格要求所有工具均有审批决定。
    const allSelected = pendingApprovals.every(
      (item) =>
        decisions[item.tool_call_id] === 'approve' ||
        decisions[item.tool_call_id] === 'reject',
    )

    if (
      !allSelected ||
      Object.keys(decisions).length !==
        pendingApprovals.length
    ) {
      setNotice('审批数据不完整，无法提交。')
      return
    }

    const expectedFingerprints: Record<string, string> = {}

    for (const item of pendingApprovals) {
      expectedFingerprints[item.tool_call_id] =
        item.fingerprint
    }

    const requestId = createRequestId()
    const receipt = { request_id: requestId, expected_fingerprints: expectedFingerprints }

    busyRef.current = true
    setIsBusy(true)
    setCanSend(false)
    setNotice('正在提交审批并等待 Agent 执行...')

    try {
      rememberReview(threadId, receipt)
      // 真正执行审批，后端会校验 fingerprint。
      const result = await reviewThreadStream(
        threadId,
        {
          decisions,
          expected_fingerprints: expectedFingerprints,
          request_id: requestId,
        },
        handleStreamEvent,
      )

      applyConfirmedReview(threadId, result, requestId)

      // 审批返回后，读取最新的真实状态和消息。
      // 历史加载失败不影响已确认的审批，也不应重新锁定会话。
      try {
      const [history, status] = await Promise.all([
        getThreadMessages(threadId),
        getThreadStatus(threadId),
      ])

      setChats((previous) =>
        previous.map((chat) =>
          chat.id === threadId
            ? {
                ...chat,
                messages: convertMessages(history.items),
              }
            : chat,
        ),
      )

      if (status.status === 'completed') {
        setPendingApprovals([])
        setCanSend(true)
        setNotice(reviewOutcomeNotice(result))
      } else if (status.status === 'pending_approval') {
        const nextApprovals = parsePendingApprovals(
          status.pending_approvals,
        )

        setPendingApprovals(nextApprovals)
        setCanSend(false)

        setNotice(
          'Agent 再次请求工具审批，请核对新的操作。',
        )
      } else {
        setPendingApprovals([])
        setCanSend(false)

        setNotice(
          `审批后的会话状态为 ${status.status}，请检查执行记录。`,
        )
      }
      } catch (error) {
        console.error('审批已确认，刷新消息失败:', error)
        setNotice(reviewOutcomeNotice(result) + ' 历史消息暂时加载失败，可检查状态刷新。')
      }

      // 历史会话列表刷新失败，不影响已确认的审批结果。
      try {
        await refreshThreads()
      } catch (error) {
        console.error('刷新会话列表失败:', error)
      }
    } catch (error) {
      console.error('审批请求或结果确认失败:', error)

      // 即使是网络错误，也不能认为审批一定未执行。
      setBlockedReviewThreads((previous) =>
        previous.includes(threadId)
          ? previous
          : [...previous, threadId],
      )

      setCanSend(false)
      setPendingApprovals([])

      setNotice(
        `审批结果尚未确认，正在只读核对原请求；请勿重复批准。请求 ID：${requestId}。`,
      )
      try {
        const recovered = await recoverReview(threadId, receipt)
        if (recovered) {
          applyConfirmedReview(threadId, recovered, requestId)
          try {
            const history = await getThreadMessages(threadId)
            setChats(previous => previous.map(chat => chat.id === threadId
              ? { ...chat, messages: convertMessages(history.items) } : chat))
          } catch {
            setNotice(reviewOutcomeNotice(recovered) + ' 历史消息暂时加载失败，可检查状态刷新。')
          }
        }
      } catch (recoveryError) {
        console.error('只读核对审批失败:', recoveryError)
        setNotice(`审批结果尚未确认，请检查会话状态；不会重新提交审批。请求 ID：${requestId}。`)
      }
    } finally {
      busyRef.current = false
      setIsBusy(false)
      setStreamActive(false)
      setStreamNode(null)
      setStreamText('')
    }
  }


  function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    const content = draft.trim()

    if (
      !content ||
      busyRef.current ||
      isLoadingHistory ||
      !canSend
    ) {
      return
    }

    void submitChat(content)
  }


  // =====================================
  // 8. 页面渲染
  // =====================================

  return (
    <div className="flex h-dvh overflow-hidden bg-white text-slate-900">

      <Sidebar
        chats={chats}
        activeId={activeId}
        activeTab={tab}
        drawerOpen={drawerOpen}
        actionsDisabled={isBusy || isLoadingHistory}
        onRenameChat={renameChat}
        onDeleteChat={deleteChat}
        onSelectChat={(id) => {
          void openChat(id)
        }}
        onNewChat={newChat}
        onChangeTab={setTab}
        onCloseDrawer={() => setDrawerOpen(false)}
      />

      <div className="flex min-w-0 flex-1 flex-col">

        <Header
          onOpenHistory={() => setDrawerOpen(true)}
          onNewChat={newChat}
          onOpenSettings={() => setSettingsOpen(true)}
        />


        {tab === 'chat' && notice && (
          <div
            role="status"
            className="shrink-0 border-b border-amber-200 bg-amber-50 px-4 py-3"
          >
            <div className="mx-auto flex max-w-3xl flex-col gap-2">
              <p className="break-words text-xs leading-5 text-amber-900">
                {notice}
              </p>

              {activeId && (
                <button
                  type="button"
                  disabled={isBusy || isLoadingHistory}
                  onClick={() => void checkThreadState()}
                  className="self-start rounded-lg border border-amber-300 bg-white px-3 py-2 text-xs font-medium text-amber-900 disabled:opacity-50"
                >
                  {isLoadingHistory
                    ? '正在检查...'
                    : '重新检查会话状态'}
                </button>
              )}
            </div>
          </div>
        )}


        {tab === 'chat' && isLoadingHistory && (
          <div
            role="status"
            className="shrink-0 px-4 py-2 text-xs text-slate-500"
          >
            正在加载历史消息...
          </div>
        )}

        <main className="flex min-h-0 flex-1 flex-col">

          {tab === 'chat' && (
            <ChatPage
              conversation={activeChat}
              draft={draft}
              onDraftChange={changeDraft}
              submissionStatus={submissionStatus}
              onSendMessage={sendMessage}
              isBusy={isBusy || isLoadingHistory}
              canSend={canSend}
              pendingApprovals={pendingApprovals}
              reviewBlocked={reviewBlocked}
              onReview={submitReview}
              streamActive={streamActive}
              streamNode={streamNode}
              streamText={streamText}
              pendingUserText={pendingUserText}
            />
          )}

          {tab === 'knowledge' && (
            <KnowledgePage />
          )}

          {tab === 'records' && (
            <RecordsPage />
          )}

        </main>

        <BottomNavigation
          activeTab={tab}
          onChange={setTab}
        />
      </div>

      {/* 设置弹窗 */}
      {settingsOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-5">
          <div
            role="dialog"
            aria-modal="true"
            aria-label="设置"
            className="w-full max-w-sm rounded-2xl bg-white p-5 shadow-xl"
          >
            <div className="mb-5 flex items-center justify-between">
              <h2 className="text-lg font-semibold">设置</h2>
              <button
                type="button"
                onClick={() => setSettingsOpen(false)}
                aria-label="关闭设置"
              >
                <X size={22} />
              </button>
            </div>

            <p className="text-sm leading-7 text-slate-500">
              模型设置、主题设置和账户管理将在后续版本加入。
            </p>

            <button
              type="button"
              onClick={() => setSettingsOpen(false)}
              className="mt-5 w-full rounded-xl bg-slate-900 py-3 text-sm text-white"
            >
              完成
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
