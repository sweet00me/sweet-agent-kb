// Chromium + actual React/App/SSE; all API requests terminate at an isolated local fixture.
import assert from 'node:assert/strict'
import { createServer as httpServer } from 'node:http'
import { pathToFileURL, fileURLToPath } from 'node:url'
import { createServer as viteServer } from 'vite'

const { chromium } = await import(process.env.SWEET_PLAYWRIGHT_CORE_PATH
  ? pathToFileURL(process.env.SWEET_PLAYWRIGHT_CORE_PATH).href : 'playwright-core')
const root = fileURLToPath(new URL('../', import.meta.url))
const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
const gate = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
let fixture
const api = httpServer(async (req, res) => {
  const state = fixture, path = new URL(req.url, 'http://localhost').pathname
  const json = (data, status = 200) => { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(data)) }
  try {
    const chunks = []; for await (const chunk of req) chunks.push(chunk)
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : null
    if (path === '/api/v1/threads') {
      if (req.method === 'POST') {
        if (state.scenario === 'create_failure') return json({ detail: 'fixture create failed' }, 503)
        const item = { thread_id: 'chat-created', title: body.title, created_at: '2026-10-22', updated_at: '2026-10-22', message_count: 0 }
        state.threads.push(item); return json(item)
      }
      return json({ items: state.threads })
    }
    const thread = path.split('/')[4]
    if (path.endsWith('/messages')) {
      if (state.scenario === 'history_failure' && state.completed && !state.historyFailed) {
        state.historyFailed = true; return json({ detail: 'fixture history unavailable' }, 503)
      }
      return json({ items: state.messages.filter(item => item.thread_id === thread), next_before_id: null })
    }
    if (path.endsWith('/status')) return json({ thread_id: thread, status: 'completed', message_count: state.messages.length, pending_approvals: [] })
    if (path.startsWith('/api/v1/requests/')) {
      const request = state.posts[0]
      assert.equal(path.split('/').at(-1), request.request_id)
      return json({ request_id: request.request_id, thread_id: request.thread_id, operation: 'chat',
        journal_status: state.completed ? 'completed' : 'running', has_saved_response: state.completed, response: state.saved ?? null })
    }
    if (path === '/api/v1/chat/stream') {
      state.posts.push(body)
      if (state.scenario === 'rejected') return json({ detail: 'fixture rejected before acceptance' }, 422)
      if (state.scenario === 'before_disconnect') { res.destroy(); return }
      await state.accept.promise
      state.messages.push({ id: state.messages.length + 1, thread_id: body.thread_id, role: 'user', content: body.message, created_at: '2026-10-22' })
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' })
      const lostAck = state.scenario.startsWith('ack_lost')
      if (lostAck) res.end()
      else res.write(frame('started', { thread_id: body.thread_id, request_id: body.request_id, status: 'running' }))
      await state.finish.promise
      if (state.scenario === 'after_disconnect') { res.end(); return }
      if (state.scenario === 'execution_error') { res.end(frame('error', { thread_id: body.thread_id, message: 'fixture execution failed' })); return }
      const rag = state.scenario === 'rag'
      const answer = rag ? '模拟材料的内容。〔1〕' : '模拟普通回答。'
      const citation_report = rag
        ? { status: 'verified', reason: 'checks_passed', claim_count: 1, mode: 'rag', citations: [{ source: 'notes/a.md', chunk_id: 'a'.repeat(32), file_sha256: 'b'.repeat(64), chunk_index: 0, quotes: ['模拟材料的内容。'] }] }
        : { status: 'not_applicable', reason: 'no_knowledge_lookup', claim_count: 0, mode: 'plain', citations: [] }
      state.messages.push({ id: state.messages.length + 1, thread_id: body.thread_id, role: 'assistant', content: answer, created_at: '2026-10-22' })
      state.completed = true
      state.saved = { thread_id: body.thread_id, status: 'completed', answer, pending_approvals: [], citation_report }
      if (!lostAck) res.end(frame('token', { text: answer }) + frame('completed', state.saved))
      return
    }
    state.unexpected.push(`${req.method} ${path}`); json({ detail: 'unknown fixture route' }, 404)
  } catch (error) { state.unexpected.push(String(error)); if (!res.headersSent) json({}, 500); else res.end() }
})
await new Promise(resolve => api.listen(0, '127.0.0.1', resolve))
const target = `http://127.0.0.1:${api.address().port}`
const vite = await viteServer({ root, configFile: root + '/vite.config.ts', server: {
  host: '127.0.0.1', port: 0, strictPort: false, proxy: { '/api': { target, changeOrigin: true } },
} })
assert.equal(vite.config.server.proxy['/api'].target, target, 'The fixture must never proxy to production')
await vite.listen()
const base = `http://127.0.0.1:${vite.httpServer.address().port}`
const browser = await chromium.launch({ headless: true })
const results = []
const scenarios = (process.env.SWEET_CHAT_UI_CASES || 'plain,rag,accepted,enter,ime,duplicates,sequential,rejected,create_failure,new_thread,before_disconnect,after_disconnect,execution_error,history_failure,ack_lost_saved,ack_lost_running,switch_draft,mobile').split(',')
try {
  for (const scenario of scenarios) {
    fixture = { scenario, accept: gate(), finish: gate(), posts: [], messages: [], unexpected: [], completed: false,
      threads: ['one', 'two'].map((id, index) => ({ thread_id: 'chat-' + id, title: `输入框回归${index + 1}`, created_at: '2026-10-22', updated_at: '2026-10-22', message_count: 0 })) }
    const state = fixture, context = await browser.newContext({ viewport: scenario === 'mobile' ? { width: 390, height: 844 } : { width: 1440, height: 900 } })
    const page = await context.newPage(); page.setDefaultTimeout(3500)
    const pageErrors = []; page.on('pageerror', error => pageErrors.push(error.message))
    await page.route('**/*', route => ['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname) ? route.continue() : route.abort())
    try {
      await page.goto(base)
      if (scenario === 'mobile') {
        await page.getByRole('button', { name: '打开历史聊天', exact: true }).click()
        await page.locator('span:visible').filter({ hasText: /^输入框回归1$/ }).click()
      } else if (!['create_failure', 'new_thread'].includes(scenario)) await page.getByText('输入框回归1', { exact: true }).click()
      const input = page.getByRole('textbox', { name: '聊天消息', exact: true })
      const send = page.getByRole('button', { name: '发送消息', exact: true })
      const content = `未提交草稿 ${scenario}`
      await input.fill(content)
      assert.equal(await input.inputValue(), content)
      if (scenario === 'ime') {
        const cdp = await context.newCDPSession(page)
        await cdp.send('Input.imeSetComposition', { text: '中文输入', selectionStart: 4, selectionEnd: 4 })
        await input.dispatchEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 229, isComposing: true, bubbles: true })
        assert.equal(state.posts.length, 0, 'Enter must not submit an active IME composition')
        await cdp.send('Input.insertText', { text: '中文输入' })
        await input.press('Enter')
      } else if (scenario === 'enter') {
        await input.press('Shift+Enter')
        assert.equal(state.posts.length, 0)
        assert.ok((await input.inputValue()).includes('\n'))
        await input.press('Enter')
      } else if (scenario === 'duplicates') {
        await send.evaluate(button => { button.click(); button.click(); button.form.requestSubmit() })
      } else await send.click()
      if (scenario === 'create_failure') {
        await page.waitForFunction(() => document.querySelector('textarea')?.disabled === false)
        assert.equal(await input.inputValue(), content)
        assert.equal(state.posts.length, 0)
      } else {
        await page.waitForFunction(() => document.querySelector('textarea')?.disabled === true)
        await assert.doesNotReject(async () => {
          const deadline = Date.now() + 3000
          while (!state.posts.length && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10))
          assert.equal(state.posts.length, 1)
        })
        if (['rejected', 'before_disconnect'].includes(scenario)) {
          await page.getByText(/草稿|请求结果尚未确认|消息尚未提交/).first().waitFor()
          assert.equal(await input.inputValue(), content)
        } else {
          assert.ok((await input.inputValue()).trim(), 'Keep the draft until the server acknowledges acceptance')
          state.accept.resolve()
          if (scenario.startsWith('ack_lost')) {
            await page.getByText(/草稿已保留/).first().waitFor()
            assert.equal(await input.inputValue(), content)
            if (scenario === 'ack_lost_running') {
              await page.getByRole('button', { name: /检查.*状态/ }).click()
              await page.getByText(/原请求.*尚未确认/).first().waitFor()
              assert.equal(await input.isDisabled(), true, 'Idle checkpoint alone must not permit resubmitting an unconfirmed request')
              assert.equal(await input.inputValue(), content)
            }
            state.finish.resolve()
            const deadline = Date.now() + 3000
            while (!state.completed && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10))
            assert.equal(state.completed, true)
            await page.getByRole('button', { name: /检查.*状态/ }).click()
            await page.waitForFunction(() => document.querySelector('textarea')?.value === '' && document.querySelector('textarea')?.disabled === false)
            assert.equal(state.posts.length, 1, 'Journal recovery must never POST the original message again')
            results.push({ scenario, passed: true, chat_posts: state.posts.length })
            continue
          }
          if (scenario === 'accepted') {
            await page.waitForFunction(() => document.querySelector('textarea')?.value === '')
            assert.equal(state.completed, false, 'Clearing must not wait for terminal SSE')
          }
          state.finish.resolve()
          if (['after_disconnect', 'execution_error'].includes(scenario)) {
            await page.getByText(/结果尚未确认|执行状态|消息已接收/).first().waitFor()
            assert.equal(await input.inputValue(), '')
          } else {
            await page.waitForFunction(() => document.querySelector('textarea')?.value === '')
            if (scenario === 'history_failure') {
              await page.getByText(/历史.*失败/).first().waitFor()
              await page.getByRole('button', { name: /检查.*状态/ }).click()
            }
            await page.waitForFunction(() => document.querySelector('textarea')?.disabled === false)
            if (scenario === 'sequential') {
              await input.fill('第二条消息'); await send.click()
              await page.waitForFunction(() => document.querySelector('textarea')?.disabled === false && document.querySelector('textarea')?.value === '')
              assert.equal(state.posts.length, 2)
              assert.notEqual(state.posts[0].request_id, state.posts[1].request_id)
            }
            if (scenario === 'switch_draft') {
              await input.fill('未发送的下一条草稿')
              await page.getByText('输入框回归2', { exact: true }).click()
              assert.equal(await input.inputValue(), '')
              await page.getByText('输入框回归1', { exact: true }).click()
              assert.equal(await input.inputValue(), '未发送的下一条草稿')
            } else if (scenario !== 'mobile') {
              await page.getByText('输入框回归2', { exact: true }).click()
              await page.getByText('输入框回归1', { exact: true }).click()
              assert.equal(await input.inputValue(), '', 'History/thread changes must not resurrect sent content')
            }
          }
        }
        assert.equal(state.posts.length, scenario === 'sequential' ? 2 : 1)
      }
      assert.deepEqual(pageErrors, [])
      assert.deepEqual(state.unexpected, [])
      results.push({ scenario, passed: true, chat_posts: state.posts.length })
    } catch (error) { results.push({ scenario, passed: false, error: String(error) }) }
    finally { state.accept.resolve(); state.finish.resolve(); await context.close() }
  }
  console.log(JSON.stringify({ chromium: browser.version(), passed: results.filter(result => result.passed).length, total: results.length, results }))
  if (results.some(result => !result.passed)) process.exitCode = 1
} finally {
  await browser.close(); await vite.close(); await new Promise(resolve => api.close(resolve))
}
