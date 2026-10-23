import assert from 'node:assert/strict'
import test from 'node:test'
import { moduleUrl, agentStreamUrl } from './load-services.mjs'

const apiUrl = await moduleUrl('../src/services/api.ts')
const recoveryUrl = await moduleUrl('../src/services/reviewRecovery.ts', {
  "from './api'": `from '${apiUrl}'`, "from './agentStream'": `from '${agentStreamUrl}'`,
})
const { recoverReview, rememberReview, getReviewReceipt, forgetReview, reviewOutcomeNotice } = await import(recoveryUrl)
const thread = 'review-recovery'
const receipt = { request_id: 'req-review-test', expected_fingerprints: { 'old-call': 'f'.repeat(64) } }
const blocked = { thread_id: thread, status: 'completed', answer: '云端未获独立授权，分析未执行。', pending_approvals: [],
  citation_report: { status: 'not_applicable', mode: 'document_analysis', analysis_status: 'blocked', analysis_id: null,
    analysis_ids: [], analysis_complete: false, sources: [], real_world_truth: 'not_checked',
    analysis_error_code: 'workflow_cloud_not_authorized', analysis_error_message: '云端未获独立授权，分析未执行。' } }
const diagnostic = { request_id: receipt.request_id, thread_id: thread, operation: 'review', journal_status: 'completed',
  checkpoint_status: 'idle', has_saved_response: true, response: blocked, automatic_replay_allowed: false }

test('断线后仅 GET 原请求已保存结果，不重发审批', async t => {
  const calls = []
  t.mock.method(globalThis, 'fetch', async (path, options = {}) => {
    calls.push({ path, method: options.method ?? 'GET' })
    return new Response(JSON.stringify(diagnostic))
  })
  const result = await recoverReview(thread, receipt)
  assert.equal(result.citation_report.analysis_status, 'blocked')
  assert.deepEqual(calls, [{ path: '/api/v1/requests/req-review-test', method: 'GET' }])
  assert.match(reviewOutcomeNotice(result), /未获独立授权/)
  assert.doesNotMatch(reviewOutcomeNotice(result), /执行成功|证据不足/)
})

test('Checkpoint 空闲不能证明 uncertain/running 请求已确认', async t => {
  for (const journal_status of ['accepted', 'running', 'uncertain']) {
    t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({ ...diagnostic,
      journal_status, has_saved_response: false, response: null })))
    assert.equal(await recoverReview(thread, receipt), null)
  }
})

test('匹配的新审批可恢复，旧调用不得重新变成可批准卡片', async t => {
  const approval = { tool_call_id: 'next-call', tool_name: 'analyze_document_collection', fingerprint: 'a'.repeat(64), arguments: {} }
  const pending = { ...blocked, status: 'pending_approval', answer: null, citation_report: null, pending_approvals: [approval] }
  t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({ ...diagnostic,
    journal_status: 'pending_approval', response: pending })))
  assert.equal((await recoverReview(thread, receipt)).pending_approvals[0].tool_call_id, 'next-call')
  pending.pending_approvals[0] = { ...approval, tool_call_id: 'old-call' }
  await assert.rejects(recoverReview(thread, receipt), /旧|重复|原审批/)
})

test('错请求、错会话、错操作或伪终止响应不能解除重复审批保护', async t => {
  for (const change of [ { request_id: 'req-other' }, { thread_id: 'other-thread' }, { operation: 'chat' },
    { response: { ...blocked, thread_id: 'other-thread' } }, { response: { ...blocked, status: 'pending_approval' } } ]) {
    t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({ ...diagnostic, ...change })))
    await assert.rejects(recoverReview(thread, receipt))
  }
})

test('未确认回执可跨模块重新加载，明确确认后才清除', async t => {
  const data = new Map()
  const original = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: {
    getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key),
  } })
  t.after(() => original ? Object.defineProperty(globalThis, 'sessionStorage', original) : delete globalThis.sessionStorage)
  rememberReview(thread, receipt)
  const reloaded = await import(recoveryUrl + '#reload')
  assert.deepEqual(reloaded.getReviewReceipt(thread), receipt)
  reloaded.forgetReview(thread)
  assert.equal(reloaded.getReviewReceipt(thread), null)
  forgetReview(thread)
  assert.equal(getReviewReceipt(thread), null)
})

test('浏览器存储无法写入或删除时，当前页的回执和确认仍然有效', t => {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: {
    getItem: () => JSON.stringify(receipt),
    setItem() { throw new Error('quota exceeded') }, removeItem() { throw new Error('storage blocked') },
  } })
  t.after(() => original ? Object.defineProperty(globalThis, 'sessionStorage', original) : delete globalThis.sessionStorage)
  const current = { ...receipt, request_id: 'req-current-review' }
  rememberReview('storage-unavailable', current)
  assert.deepEqual(getReviewReceipt('storage-unavailable'), current)
  forgetReview('storage-unavailable')
  assert.equal(getReviewReceipt('storage-unavailable'), null)
})
