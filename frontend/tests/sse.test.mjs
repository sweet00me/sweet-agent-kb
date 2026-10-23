import assert from 'node:assert/strict'
import test from 'node:test'
import { postSse, runAgentStream } from './load-services.mjs'

const thread = 'research-sse-test'
const report = {
  status: 'verified', reason: 'checks_passed', claim_count: 1,
  checked_at: '2026-10-21T09:00:00+00:00',
  citations: [{ source: 'notes/检索笔记.md', chunk_id: 'a'.repeat(32), chunk_index: 0,
    file_sha256: 'b'.repeat(64), quotes: ['RAG 通过检索文档补充生成上下文。'] }],
}
const completed = { thread_id: thread, status: 'completed', answer: 'RAG 结合检索与生成。〔1〕',
  pending_approvals: [], citation_report: report }
const frame = (event, data, ending = '\n') =>
  `event: ${event}${ending}data: ${JSON.stringify(data)}${ending}${ending}`

function response(text, { bytewise = false, status = 200, type = 'text/event-stream' } = {}) {
  const bytes = new globalThis.TextEncoder().encode(text)
  const stream = new globalThis.ReadableStream({
    start(controller) {
      if (bytewise) {
        for (let i = 0; i < bytes.length; i++) controller.enqueue(bytes.slice(i, i + 1))
      } else controller.enqueue(bytes)
      controller.close()
    },
  })
  return new globalThis.Response(stream, { status, headers: { 'Content-Type': type } })
}

function mockFetch(t, result) {
  return t.mock.method(globalThis, 'fetch', async () => result)
}

test('UTF-8/CRLF 单字节分片仍保留新 RAG 引用报告', async t => {
  mockFetch(t, response(': 心跳\r\n\r\n' + frame('started', { thread_id: thread }, '\r\n')
    + frame('token', { text: completed.answer }, '\r\n') + frame('completed', completed, '\r\n'), { bytewise: true }))
  const events = []
  const result = await runAgentStream('/api/v1/chat/stream', {}, thread, event => events.push(event))
  assert.equal(result.answer, completed.answer)
  assert.deepEqual(result.citation_report, report)
  assert.deepEqual(events.map(event => event.event), ['started', 'token', 'completed'])
})

test('证据不足也是完成结果，拒答报告必须传给前端', async t => {
  const rejected = { ...completed, answer: '证据不足，无法确认。',
    citation_report: { status: 'rejected', reason: 'no_evidence', claim_count: 0, citations: [] } }
  mockFetch(t, response(frame('completed', rejected)))
  const result = await runAgentStream('/api/v1/chat/stream', {}, thread, () => {})
  assert.deepEqual(result.citation_report, rejected.citation_report)
})

test('当前 plain 与 rag 报告均能结束流，不能误判成连接失败', async t => {
  for (const citation_report of [
    { status: 'not_applicable', reason: 'no_knowledge_lookup', claim_count: 0, citations: [], mode: 'plain' },
    { ...report, mode: 'rag' },
  ]) {
    await t.test(citation_report.mode, async sub => {
      mockFetch(sub, response(frame('completed', { ...completed, citation_report })))
      const result = await runAgentStream('/api/v1/chat/stream', {}, thread, () => {})
      assert.equal(result.citation_report.mode, citation_report.mode)
    })
  }
})

test('started 必须匹配发送请求，不能确认其他请求已接收', async t => {
  mockFetch(t, response(frame('started', { thread_id: thread, request_id: 'wrong-request', status: 'running' }) + frame('completed', completed)))
  let acknowledged = false
  await assert.rejects(runAgentStream('/api/v1/chat/stream', { request_id: 'correct-request' }, thread,
    event => { if (event.event === 'started') acknowledged = true }), /请求|request/i)
  assert.equal(acknowledged, false)
})

test('plain/rag 模式不能绕过引用状态与来源校验', async t => {
  for (const citation_report of [
    { ...report, mode: 'plain' },
    { status: 'not_applicable', reason: 'bad', claim_count: 0, citations: [], mode: 'rag' },
    { ...report, mode: 'unknown' },
  ]) {
    mockFetch(t, response(frame('completed', { ...completed, citation_report })))
    await assert.rejects(runAgentStream('/api/v1/chat/stream', {}, thread, () => {}), /引用|核验/)
  }
})

test('审批暂停和旧响应允许空报告或缺省报告', async t => {
  mockFetch(t, response(frame('pending_approval', { thread_id: thread, status: 'pending_approval',
    answer: null, pending_approvals: [{ tool_call_id: 'save-one', tool_name: 'simulate_save_note' }], citation_report: null })))
  const result = await runAgentStream('/api/v1/chat/stream', {}, thread, () => {})
  assert.equal(result.status, 'pending_approval')
  assert.equal(result.citation_report, null)
})

test('错误的引用报告不能被标为已通过核验', async t => {
  mockFetch(t, response(frame('completed', { ...completed,
    citation_report: { ...report, citations: [], claim_count: 0 } })))
  await assert.rejects(runAgentStream('/api/v1/chat/stream', {}, thread, () => {}), /引用|核验/)
})

test('引用状态、Chunk 标识、哈希和引文必须满足接口类型', async t => {
  const badReports = [
    { ...report, status: ['verified'] },
    { ...report, citations: [{ ...report.citations[0], chunk_id: 'forged-id' }] },
    { ...report, citations: [{ ...report.citations[0], file_sha256: null }] },
    { ...report, citations: [{ ...report.citations[0], chunk_index: -1 }] },
    { ...report, citations: [{ ...report.citations[0], quotes: [42] }] },
  ]
  for (const citation_report of badReports) {
    await t.test(JSON.stringify(citation_report).slice(0, 80), async sub => {
      mockFetch(sub, response(frame('completed', { ...completed, citation_report })))
      await assert.rejects(runAgentStream('/api/v1/chat/stream', {}, thread, () => {}), /引用/)
    })
  }
})

test('旧后端未提供引用字段时兼容正常完成', async t => {
  const legacy = { ...completed }
  delete legacy.citation_report
  mockFetch(t, response(frame('completed', legacy)))
  const result = await runAgentStream('/api/v1/chat/stream', {}, thread, () => {})
  assert.equal(result.answer, completed.answer)
  assert.equal(result.citation_report, undefined)
})

test('只有 started 后断线不能当作完成，也不自动重发', async t => {
  const fetch = mockFetch(t, response(frame('started', { thread_id: thread })))
  await assert.rejects(runAgentStream('/api/v1/chat/stream', {}, thread, () => {}), /没有收到/)
  assert.equal(fetch.mock.callCount(), 1)
})

test('残缺事件、JSON 错误、错线程、重复完成都拒绝', async t => {
  for (const text of [
    frame('completed', completed).slice(0, -1),
    'event: completed\ndata: {broken}\n\n',
    frame('completed', { ...completed, thread_id: 'other-thread' }),
    frame('started', { thread_id: 'other-thread' }),
    frame('completed', { ...completed, status: 'pending_approval' }),
    frame('completed', completed) + frame('completed', completed),
  ]) {
    await t.test(text.slice(0, 45), async sub => {
      mockFetch(sub, response(text))
      await assert.rejects(runAgentStream('/api/v1/chat/stream', {}, thread, () => {}))
    })
  }
})

test('后端执行异常事件会拒绝，不能以 HTTP 200 当成功', async t => {
  mockFetch(t, response(frame('error', { thread_id: thread, message: 'Agent 执行状态无法确认' })))
  await assert.rejects(runAgentStream('/api/v1/chat/stream', {}, thread, () => {}), /无法确认/)
})

test('HTTP 错误和代理 HTML 不会进入 SSE 解析或自动重试', async t => {
  for (const options of [{ status: 409, type: 'application/json' }, { type: 'text/html' }]) {
    await t.test(JSON.stringify(options), async sub => {
      const fetch = mockFetch(sub, response('unavailable', options))
      await assert.rejects(postSse({ path: '/api/v1/chat/stream', payload: {}, onEvent() {} }), /SSE 请求失败|不是 SSE/)
      assert.equal(fetch.mock.callCount(), 1)
    })
  }
})

test('集合分析未授权是已确认的工具失败，兼容已保存的旧报告', async t => {
  const analysis = { status: 'not_applicable', mode: 'document_analysis', analysis_status: 'blocked',
    analysis_id: null, analysis_ids: [], analysis_complete: false, sources: [], real_world_truth: 'not_checked' }
  mockFetch(t, response(frame('completed', { ...completed, answer: '集合分析未执行：云端未授权。', citation_report: analysis })))
  const result = await runAgentStream('/api/v1/threads/review/stream', {}, thread, () => {})
  assert.equal(result.status, 'completed')
  assert.equal(result.citation_report.analysis_status, 'blocked')
  assert.equal(result.citation_report.analysis_complete, false)
  assert.match(result.answer, /未授权/)
})

test('混合 RAG 和集合分析保留两类核验结果', async t => {
  const analysis = { status: 'not_applicable', reason: 'analysis_failed', claim_count: 0, citations: [],
    mode: 'document_analysis', analysis_status: 'failed', analysis_id: null, analysis_ids: [],
    analysis_complete: false, sources: [], real_world_truth: 'not_checked', analysis_error_code: 'analysis_tool_failed' }
  mockFetch(t, response(frame('completed', { ...completed,
    citation_report: { ...report, mode: 'rag_and_document_analysis', document_analysis: analysis } })))
  const result = await runAgentStream('/api/v1/chat/stream', {}, thread, () => {})
  assert.equal(result.citation_report.status, 'verified')
  assert.equal(result.citation_report.document_analysis.analysis_error_code, 'analysis_tool_failed')
})

test('不能使用集合分析格式绕过 RAG 校验或伪造完成状态', async t => {
  for (const bad of [
    { status: 'verified', mode: 'document_analysis', analysis_status: 'blocked', analysis_complete: false, sources: [], real_world_truth: 'not_checked' },
    { status: 'not_applicable', mode: 'document_analysis', analysis_status: 'blocked', analysis_id: null, analysis_ids: [], analysis_complete: true, sources: [], real_world_truth: 'not_checked' },
  ]) {
    mockFetch(t, response(frame('completed', { ...completed, citation_report: bad })))
    await assert.rejects(runAgentStream('/review/stream', {}, thread, () => {}))
  }
})

test('完整分析的 Evidence 来源与原文位置保留，格式错误不能冒充有效来源', async t => {
  const source = { evidence_id: 'c'.repeat(64), source: 'notes/a.md', document_sha256: 'd'.repeat(64),
    location: { start_char: 0, end_char: 2, start_line: 1, end_line: 1, start_column: 1, end_column: 3 } }
  const analysis = { status: 'not_applicable', reason: 'document_analysis_report', claim_count: 0, citations: [],
    mode: 'document_analysis', analysis_status: 'completed', analysis_complete: true,
    analysis_id: 'e'.repeat(64), analysis_ids: ['e'.repeat(64)], sources: [source], real_world_truth: 'not_checked' }
  mockFetch(t, response(frame('completed', { ...completed, citation_report: analysis })))
  assert.deepEqual((await runAgentStream('/review/stream', {}, thread, () => {})).citation_report.sources, [source])
  for (const bad of [{ ...source, document_sha256: 'forged' }, { ...source, location: { ...source.location, end_char: 0 } }]) {
    mockFetch(t, response(frame('completed', { ...completed, citation_report: { ...analysis, sources: [bad] } })))
    await assert.rejects(runAgentStream('/review/stream', {}, thread, () => {}), /来源|位置/)
  }
})
