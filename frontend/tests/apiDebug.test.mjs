import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { ref, computed, watch, effectScope, nextTick } from 'vue'
import { apiError, copy, requestSignatures, assertionSignature, localRequestStep, literal } from '../src/utils/apiTesting.js'
import { pollRequestConfig } from '../src/utils/polling.js'

// Load the production composable unchanged except for module dependencies.
// Vue's actual reactive/watch implementation drives every race below.
const source = readFileSync(new URL('../src/composables/useApiDebug.js', import.meta.url), 'utf8')
  .replace(/^import .*\n/gm, '').replace('export function useApiDebug', 'function useApiDebug')
const loadComposable = new Function('ref', 'computed', 'watch', 'onBeforeUnmount', 'ElMessage', 'ElMessageBox',
  'api', 'createUuid', 'apiError', 'copy', 'requestSignatures', 'assertionSignature', 'pollRequestConfig', `${source}\nreturn useApiDebug;`)
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
async function settle() { await nextTick(); for (let i = 0; i < 12; i++) await Promise.resolve() }
function response(status = 'PASS', value = 'original') {
  return { data: { status, results: { one: { step_id: 'one', status, captured_at: '2026-10-02T12:00:00',
    detail: { response: { status_code: 200, body: { value } }, assertions: [], error: null } } } } }
}
function setup(t, overrides = {}) {
  const calls = [], deleted = [], messages = [], lifecycle = [], scope = effectScope()
  let created = 0
  const api = { apiTesting: {
    async post(path, payload) {
      calls.push({ path, payload: copy(payload) })
      if (overrides.post) {
        const result = overrides.post(path, payload)
        if (result !== undefined) return result
      }
      if (path === '/debug-sessions') return { data: { id: `session-${++created}` } }
      return { data: {} }
    },
    async get(path) { calls.push({ path }); return overrides.get ? overrides.get(path) : response() },
    async delete(path) { deleted.push(path); return overrides.delete ? overrides.delete(path) : {} },
  } }
  const message = Object.fromEntries(['error', 'warning', 'success'].map(kind => [kind, text => messages.push({ kind, text })]))
  const confirm = overrides.confirm || (() => Promise.resolve())
  const useApiDebug = loadComposable(ref, computed, watch, callback => lifecycle.push(callback), message, { confirm },
    api, () => 'editor-one', apiError, copy, requestSignatures, assertionSignature, pollRequestConfig)
  const step = localRequestStep(); step.id = 'one'; step.snapshot.request.url = literal('https://old.example.test')
  const steps = ref([step]), env = ref(1)
  const debug = scope.run(() => useApiDebug(env, steps, { retainDisplay: true, envName: () => `环境${env.value}` }))
  const unmount = () => { lifecycle.forEach(fn => fn()); scope.stop() }
  t.after(unmount)
  return { debug, steps, env, calls, deleted, messages, unmount }
}

test('debug locks before confirmation and a draft edit cancels confirmation without sending HTTP', async t => {
  const confirm = deferred()
  const state = setup(t, { confirm: () => confirm.promise })
  const first = state.debug.run('one')
  assert.equal(state.debug.running.value, true)
  await state.debug.run('one')
  state.steps.value[0].snapshot.request.url = literal('https://new.example.test')
  confirm.resolve(); await first
  assert.equal(state.debug.running.value, false)
  assert.equal(state.calls.length, 0)
})

test('a draft edit while execute is pending never labels the old response as current', async t => {
  const execute = deferred()
  const state = setup(t, { post: path => path.endsWith('/execute') ? execute.promise : undefined })
  const pending = state.debug.run('one'); await settle()
  const sent = state.calls.find(call => call.path.endsWith('/execute'))
  assert.equal(sent.payload.steps[0].snapshot.request.url.value, 'https://old.example.test')
  state.steps.value[0].snapshot.request.url = literal('https://new.example.test')
  execute.resolve({ data: { status: 'RUNNING' } }); await pending
  assert.equal(state.debug.results.value.one, undefined)
  assert.equal(state.debug.id.value, null)
  assert.equal(state.calls.some(call => call.path === '/debug-sessions/session-1' && !call.payload), false)
  assert.deepEqual(state.deleted, ['/debug-sessions/session-1'])
})

test('unmount during session creation deletes the late session and never executes it', async t => {
  const create = deferred()
  const state = setup(t, { post: path => path === '/debug-sessions' ? create.promise : undefined })
  const pending = state.debug.run('one'); await settle()
  state.unmount()
  create.resolve({ data: { id: 'late-session' } }); await pending
  assert.deepEqual(state.deleted, ['/debug-sessions/late-session'])
  assert.equal(state.calls.filter(call => call.path.endsWith('/execute')).length, 0)
  assert.equal(state.debug.id.value, null)
})

test('environment change during polling discards the old response and a delayed close cannot clear a new session', async t => {
  const poll = deferred(), deletion = deferred()
  let polls = 0
  const state = setup(t, {
    get: () => ++polls === 1 ? poll.promise : response('PASS', 'new-environment'),
    delete: path => path.endsWith('session-1') ? deletion.promise : {},
  })
  const oldRun = state.debug.run('one'); await settle()
  state.env.value = 2
  const newRun = state.debug.run('one'); await newRun
  assert.equal(state.debug.id.value, 'session-2')
  assert.equal(state.debug.results.value.one.env_id, 2)
  poll.resolve(response('PASS', 'old-environment')); deletion.resolve({}); await oldRun; await settle()
  assert.equal(state.debug.id.value, 'session-2')
  assert.equal(state.debug.results.value.one.detail.response.body.value, 'new-environment')
  assert.equal(state.debug.results.value.one.env_name, '环境2')
})

test('editing assertions during recheck discards stale check results and keeps the new draft pending', async t => {
  const check = deferred()
  const state = setup(t, { post: path => path.endsWith('/assertions') ? check.promise : undefined })
  await state.debug.run('one')
  state.steps.value[0].snapshot.assertions[0].op = 'eq'
  state.steps.value[0].snapshot.assertions[0].expected = literal(200)
  const pending = state.debug.recheck('one'); await settle()
  state.steps.value[0].snapshot.assertions[0].expected = literal(500)
  check.resolve({ data: { result: { ...response('FAIL').data.results.one, detail: { error: 'old-check' } } } })
  await pending
  assert.equal(state.debug.results.value.one.status, 'PASS')
  assert.equal(state.debug.results.value.one.detail.error, null)
  assert.equal(state.debug.pendingIds.value.has('one'), true)
  assert.equal(state.steps.value[0].snapshot.assertions[0].expected.value, 500)
  assert.equal(state.debug.rechecking.value, false)
  assert.equal(state.messages.some(item => item.text.includes('校验未通过')), false)
})

test('an older recheck finishing cannot unlock a newer in-flight recheck', async t => {
  const first = deferred(), second = deferred()
  let checks = 0
  const state = setup(t, { post: path => path.endsWith('/assertions') ? (++checks === 1 ? first.promise : second.promise) : undefined })
  await state.debug.run('one')
  const oldCheck = state.debug.recheck('one')
  state.steps.value[0].snapshot.assertions[0].expected = literal(201)
  const newCheck = state.debug.recheck('one')
  first.resolve({ data: { result: response().data.results.one } }); await oldCheck
  assert.equal(state.debug.rechecking.value, true)
  second.resolve({ data: { result: { ...response('UNCHECKED').data.results.one, assertions_pending: false } } }); await newCheck
  assert.equal(state.debug.rechecking.value, false)
  assert.equal(state.debug.results.value.one.status, 'UNCHECKED')
  assert.equal(state.debug.pendingIds.value.has('one'), false)
})

test('successful response stays current across metadata changes and polling failures clean up sessions', async t => {
  let fail = false
  const state = setup(t, { get: () => fail ? Promise.reject(new Error('poll failed')) : response('UNCHECKED') })
  await state.debug.run('one')
  state.steps.value[0].name = '重命名'
  state.steps.value[0].response_schema = { fields: [], source: 'debug' }
  assert.equal(state.debug.results.value.one.status, 'UNCHECKED')
  assert.equal(state.debug.staleIds.value.has('one'), false)
  fail = true
  await state.debug.run('one')
  assert.equal(state.debug.id.value, null)
  assert.equal(state.debug.running.value, false)
  assert.equal(state.debug.error.value, 'poll failed')
  assert.deepEqual(state.deleted, ['/debug-sessions/session-1'])
})
