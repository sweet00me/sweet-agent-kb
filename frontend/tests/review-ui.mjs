// 真实 React 页面，模拟 HTTP 边界；不连接正式 Agent 或模型。
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'
const modulePath = process.env.SWEET_PLAYWRIGHT_CORE_PATH
const { chromium } = await import(modulePath ? pathToFileURL(modulePath).href : 'playwright-core')
const browser = await chromium.launch({ headless: true })
const base = process.env.SWEET_REVIEW_UI_URL ?? 'http://127.0.0.1:5189'
const results = []
try {
  for (const scenario of ['blocked', 'failed', 'history_failure', 'disconnect_saved', 'disconnect_running']) {
    const context = await browser.newContext()
    const page = await context.newPage()
    page.setDefaultTimeout(5000)
    const thread = 'review-ui-' + scenario
    let posts = 0, diagnostics = 0, state = 'pending_approval', journal = 'running', failHistory = scenario === 'history_failure'
    let requestId, saved
    const calls = []
    const pending = () => [{ tool_call_id: `analysis-${posts + 1}`, tool_name: 'analyze_document_collection',
      fingerprint: 'a'.repeat(64), arguments: { question: '集合分析', scope_query: 'notes' } }]
    const answer = scenario === 'failed' ? '集合分析执行失败。' : '完整文档云端分析未获独立授权，未开始分析。'
    await page.route('**/api/**', async route => {
      const request = route.request(), path = new URL(request.url()).pathname
      const json = body => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
      if (path === '/api/v1/threads') return json({ items: [{ thread_id: thread, title: '审批回归', created_at: '2026-10-22', updated_at: '2026-10-22', message_count: 1 }] })
      if (path.endsWith('/messages')) {
        if (posts && failHistory) { failHistory = false; return route.fulfill({ status: 503, body: 'temporary history failure' }) }
        return json({ items: [{ id: 1, thread_id: thread, role: 'user', content: '分析文档' },
          ...(state === 'completed' ? [{ id: 2, thread_id: thread, role: 'assistant', content: answer }] : [])], next_before_id: null })
      }
      if (path.endsWith('/status')) return json({ thread_id: thread, status: state, message_count: 2, pending_approvals: state === 'pending_approval' ? pending() : [] })
      if (path.startsWith('/api/v1/requests/')) {
        diagnostics++
        assert.equal(path.split('/').at(-1), requestId)
        return json({ request_id: requestId, thread_id: thread, operation: 'review', journal_status: journal,
          checkpoint_status: 'idle', has_saved_response: journal !== 'running', automatic_replay_allowed: false,
          response: journal !== 'running' ? saved : null })
      }
      if (path.endsWith('/review/stream')) {
        const body = request.postDataJSON()
        calls.push(Object.keys(body.decisions)); posts++
        requestId = body.request_id
        const status = scenario === 'failed' ? 'failed' : 'blocked'
        saved = { thread_id: thread, status: 'completed', answer, pending_approvals: [],
          citation_report: { status: 'not_applicable', mode: 'document_analysis', analysis_status: status,
            analysis_id: null, analysis_ids: [], analysis_complete: false, sources: [], real_world_truth: 'not_checked',
            analysis_error_message: answer, analysis_error_code: scenario === 'failed' ? 'analysis_tool_failed' : 'workflow_cloud_not_authorized' } }
        const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
        state = scenario === 'disconnect_running' && posts === 1 ? 'incomplete' : 'completed'
        journal = state === 'completed' ? 'completed' : 'running'
        const dropped = scenario.startsWith('disconnect') && posts === 1
        return route.fulfill({ status: 200, contentType: 'text/event-stream', body:
          frame('started', { thread_id: thread, request_id: requestId, status: 'running' }) + (dropped ? '' : frame('completed', saved)) })
      }
      throw new Error(`Unexpected API: ${request.method()} ${path}`)
    })
    async function approve() {
      await page.getByRole('button', { name: '批准', exact: true }).click()
      await page.getByRole('button', { name: '确认审批决定', exact: true }).click()
      await page.getByRole('button', { name: '确认并提交', exact: true }).click()
    }
    await page.goto(base)
    await page.getByText('审批回归', { exact: true }).click()
    await approve()
    if (scenario === 'disconnect_running') {
      await page.getByText(/审批结果尚未/).first().waitFor()
      assert.equal(posts, 1)
      assert.ok(diagnostics >= 1)
      await page.reload()
      await page.getByText('审批回归', { exact: true }).click()
      await page.getByText(/审批结果尚未/).first().waitFor()
      assert.equal(posts, 1)
      // 原请求保存后，当前 Checkpoint 已进入下一批审批；只允许批准新 ID。
      journal = 'completed'; state = 'pending_approval'
      await page.getByRole('button', { name: /检查.*状态/ }).click()
      await approve()
      await page.getByText(/审批已确认/).first().waitFor()
      assert.deepEqual(calls, [['analysis-1'], ['analysis-2']])
    } else {
      await page.getByText(/审批已确认/).first().waitFor()
      assert.equal(await page.getByRole('region', { name: '待审批工具' }).count(), 0)
      assert.equal(posts, 1)
      assert.ok((await page.locator('body').innerText()).includes(answer))
      if (scenario === 'disconnect_saved') assert.ok(diagnostics >= 1)
      // 已确认后的新审批不应被上一次会话锁永久禁用。
      state = 'pending_approval'
      await page.getByRole('button', { name: /检查.*状态/ }).click()
      await page.getByRole('button', { name: '批准', exact: true }).waitFor()
      assert.equal(await page.getByRole('button', { name: '批准', exact: true }).isEnabled(), true)
    }
    results.push({ scenario, passed: true, review_posts: posts, diagnostic_gets: diagnostics })
    await context.close()
  }
  console.log(JSON.stringify({ passed: results.length, results }))
} finally {
  await browser.close()
}
