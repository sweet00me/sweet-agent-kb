import assert from 'node:assert/strict'
import { createHash, randomUUID } from 'node:crypto'
import { readFile, mkdir, writeFile } from 'node:fs/promises'
import { dirname, resolve, sep } from 'node:path'
import { parseArgs } from 'node:util'
import { runAgentStream } from './load-services.mjs'

const { values } = parseArgs({ options: {
  live: { type: 'boolean', default: false },
  'base-url': { type: 'string', default: 'http://127.0.0.1:8010' },
  output: { type: 'string', default: '/tmp/sweet-agent-rag-api-sse-live.json' },
  question: { type: 'string', default: '请检索知识库中 rag_intro.md 对 RAG 的定义，仅依据这份笔记简要回答并引用。' },
} })
if (!values.live) throw new Error('真实服务验收需传 --live；npm test 仅运行离线回归。')

const base = new URL(values['base-url'])
const url = path => new URL(path, base).href
const threads = []
const results = []
const cleanup = []
const storageRoot = '/home/sweet/Storage'
let failure

async function jsonRequest(path, body, method = 'POST') {
  const response = await globalThis.fetch(url(path), { method,
    ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  })
  assert.equal(response.status, method === 'DELETE' ? 204 : 200, `API ${path}: ${response.status}`)
  return method === 'DELETE' ? null : response.json()
}

try {
  for (const stream of [false, true]) {
    const thread = 'rag-regression-' + randomUUID().replaceAll('-', '').slice(0, 16)
    threads.push(thread)
    const payload = { thread_id: thread, request_id: 'req-' + randomUUID(), message: values.question }
    const events = []
    const started = globalThis.performance.now()
    const result = stream
      ? await runAgentStream(url('/api/v1/chat/stream'), payload, thread, event => events.push(event))
      : await jsonRequest('/api/v1/chat', payload)
    assert.equal(result.status, 'completed')
    assert.equal(result.citation_report?.status, 'verified', JSON.stringify(result.citation_report))
    assert.ok(result.answer && result.citation_report.citations.length > 0)
    for (const citation of result.citation_report.citations) {
      const path = resolve(storageRoot, citation.source)
      assert.ok(path.startsWith(storageRoot + sep), '来源必须处于 Storage 内')
      const bytes = await readFile(path)
      assert.equal(createHash('sha256').update(bytes).digest('hex'), citation.file_sha256)
    }
    if (stream) {
      assert.equal(events[0].event, 'started')
      assert.equal(events.at(-1).event, 'completed')
      assert.equal(events.filter(event => event.event === 'completed').length, 1)
      const tokens = events.filter(event => event.event === 'token').map(event => event.data.text).join('')
      assert.equal(tokens, result.answer, '前端必须仅收到经过核验的正文')
      assert.ok(!tokens.includes('【') && !tokens.includes('"checks"'))
    }
    // 用另一种协议重放同一请求；报告、时间和回答必须完全保持。
    const replay = stream
      ? await jsonRequest('/api/v1/chat', payload)
      : await runAgentStream(url('/api/v1/chat/stream'), payload, thread, () => {})
    assert.deepEqual(replay, result)
    const messages = await jsonRequest(`/api/v1/threads/${thread}/messages`, undefined, 'GET')
    assert.deepEqual(messages.items.map(message => message.role), ['user', 'assistant'])
    assert.equal(messages.items.at(-1).content, result.answer)
    results.push({ transport: stream ? 'SSE' : 'HTTP', passed: true,
      duration_s: Math.round((globalThis.performance.now() - started) / 10) / 100,
      citation_status: result.citation_report.status, claim_count: result.citation_report.claim_count,
      sources: result.citation_report.citations.map(citation => citation.source),
      cross_transport_replay: true, source_sha256_matches: true,
      events: events.map(event => event.event),
    })
    process.stdout.write(JSON.stringify(results.at(-1)) + '\n')
  }
} catch (error) {
  failure = error
} finally {
  for (const thread of threads) {
    try {
      await jsonRequest(`/api/v1/threads/${thread}`, undefined, 'DELETE')
      cleanup.push({ thread_id: thread, archived: true })
    } catch (error) {
      cleanup.push({ thread_id: thread, archived: false, error: String(error) })
      failure ??= error
    }
  }
  const output = resolve(values.output)
  await mkdir(dirname(output), { recursive: true })
  await writeFile(output, JSON.stringify({ checked_at: new Date().toISOString(),
    base_url: base.href, passed: !failure, results, cleanup,
    ...(failure ? { error: String(failure) } : {}),
  }, null, 2), { mode: 0o600 })
  process.stdout.write(`验收报告：${output}\n`)
}
if (failure) throw failure
