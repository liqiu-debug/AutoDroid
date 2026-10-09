import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { ref, computed, watch, reactive, effectScope, nextTick } from 'vue'
import { apiError, copy, effectiveStep, pathLabel, assertionLabel, previewValue, localRequestStep, literal, newAssertions } from '../src/utils/apiTesting.js'

// Run production script setup with real Vue reactivity, replacing only imports,
// lifecycle registration, and the two compiler-provided setup macros.
function loadScript(name, exports) {
  const source = readFileSync(new URL(`../src/components/api-testing/${name}.vue`, import.meta.url), 'utf8')
    .match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\n/gm, '')
  return new Function('ref', 'computed', 'watch', 'onMounted', 'onBeforeUnmount', 'nextTick', 'ElMessage',
    'api', 'createUuid', 'apiError', 'copy', 'effectiveStep', 'pathLabel', 'assertionLabel', 'previewValue', 'newAssertions',
    'defineProps', 'defineEmits', `${source}\nreturn {${exports.join(',')}};`)
}
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
async function settle() { await nextTick(); for (let i = 0; i < 10; i++) await Promise.resolve() }
function aiResponse(payload, callId = 'call-one') {
  return { data: { call_id: callId, draft_token: payload.draft_token, suggestions: [
    { assertion: { id: 'model-id', path: ['body', 'count'], op: 'type', expected: literal('number') }, reason: '检查类型', evidence: { path: ['body', 'count'], type: 'number' } },
  ], warnings: [] } }
}
function explanation(id) {
  return { data: { call_id: `call-${id}`, facts: [{ id: 'F1', step_id: id, path: [], message: `report-${id}` }], possible_causes: [], next_steps: [], warnings: [] } }
}
function setup(t, name = 'AiAssertions', overrides = {}) {
  const calls = [], events = [], messages = [], mounted = [], lifecycle = [], scope = effectScope()
  let sequence = 0
  const step = localRequestStep(); step.id = 'one'; step.snapshot.request.url = literal('https://demo.example')
  const props = reactive({ steps: [step], stepId: 'one', envId: 1, debugSessionId: 'debug-one', editorId: 'editor-one',
    result: { captured_at: '2026-10-02T00:00:00', detail: { response: { status_code: 200 } } }, disabled: false, runId: 'run-one' })
  const api = { apiTesting: {
    async get(path) { calls.push({ path }); return { data: { available: true } } },
    async post(path, payload) {
      calls.push({ path, payload: copy(payload) })
      const custom = overrides.post?.(path, payload)
      if (custom !== undefined) return custom
      return path === '/ai/suggest-assertions' ? aiResponse(payload) : path === '/ai/explain-failure' ? explanation(payload.run_id) : { data: { ok: true } }
    },
  } }
  const emit = (event, payload) => {
    events.push({ event, payload: copy(payload) })
    if (event === 'update-assertions') {
      const target = props.steps.find(row => row.id === props.stepId)
      target.overrides = { ...target.overrides, assertions: copy(payload) }
    }
  }
  const names = name === 'AiAssertions' ? ['available','open','busy','goal','suggestions','chosen','warnings','error','callId','revision','resultRevision','applied','assertions','stale','generate','apply','undo','dismiss'] : ['available','open','busy','data','error','explain','jump']
  const script = loadScript(name, names)
  const state = scope.run(() => script(ref, computed, watch, fn => mounted.push(fn), fn => lifecycle.push(fn), nextTick,
    Object.fromEntries(['error','warning','success'].map(kind => [kind, text => messages.push({ kind, text })])),
    api, () => `uuid-${++sequence}`, apiError, copy, effectiveStep, pathLabel, assertionLabel, previewValue, newAssertions, () => props, () => emit))
  const unmount = () => { lifecycle.forEach(fn => fn()); scope.stop() }
  t.after(unmount)
  return { state, props, calls, events, messages, unmount, mount: () => Promise.all(mounted.map(fn => fn())) }
}
const feedbacks = context => context.calls.filter(call => call.path === '/ai/feedback')

test('AI suggestions with an outdated draft token never update assertions', async t => {
  const context = setup(t)
  await context.state.generate()
  context.state.chosen.value = [0]
  context.props.steps[0].snapshot.request.url = literal('https://changed.example')
  await context.state.apply()
  assert.equal(context.state.stale.value, true)
  assert.equal(context.events.length, 0)
  assert.equal(feedbacks(context).length, 0)
})

test('changing the response while generation is pending makes its late suggestions unusable', async t => {
  const pending = deferred()
  const context = setup(t, 'AiAssertions', { post: path => path === '/ai/suggest-assertions' ? pending.promise : undefined })
  const work = context.state.generate()
  const sent = context.calls.find(call => call.payload?.draft_token)
  context.props.result.captured_at = '2026-10-02T00:01:00'
  pending.resolve(aiResponse(sent.payload)); await work
  context.state.chosen.value = [0]
  await context.state.apply()
  assert.equal(context.events.length, 0)
  assert.match(context.state.error.value, /重新生成/)
})

test('changing the business goal also invalidates generated suggestions', async t => {
  const context = setup(t)
  await context.state.generate(); context.state.chosen.value = [0]
  context.state.goal.value = '改为校验订单已经支付'
  await context.state.apply()
  assert.equal(context.events.length, 0)
  assert.equal(context.state.stale.value, true)
})

test('undo removes only accepted suggestions and preserves subsequent unrelated manual assertions', async t => {
  const context = setup(t)
  await context.state.generate(); context.state.chosen.value = [0]; await context.state.apply()
  const manual = { id: 'manual-after-ai', path: ['body','total'], op: 'exists', expected: literal(null) }
  context.props.steps[0].overrides.assertions.push(manual)
  await settle(); context.state.undo(); await settle()
  assert.ok(context.state.assertions.value.some(item => item.id === manual.id))
  assert.equal(context.state.assertions.value.filter(item => item.path.includes('count')).length, 0)
  assert.deepEqual(feedbacks(context).map(call => call.payload.action), ['accepted','undone'])
})

test('undo cannot erase an AI suggestion that the user edited after applying it', async t => {
  const context = setup(t)
  await context.state.generate(); context.state.chosen.value = [0]; await context.state.apply()
  const inserted = context.props.steps[0].overrides.assertions.at(-1)
  inserted.op = 'exists'; inserted.expected = literal(null)
  await settle(); context.state.undo()
  assert.ok(context.state.assertions.value.some(item => item.id === inserted.id && item.op === 'exists'))
  assert.equal(context.events.length, 1)
  assert.ok(context.messages.some(item => item.kind === 'warning'))
})

test('the same AI generation cannot be applied twice by repeated clicks', async t => {
  const context = setup(t)
  await context.state.generate(); context.state.chosen.value = [0]
  await Promise.all([context.state.apply(), context.state.apply()])
  assert.equal(context.events.length, 1)
  assert.equal(feedbacks(context).filter(call => call.payload.action === 'accepted').length, 1)
})

test('reopening and applying the same suggestions again never duplicates assertions', async t => {
  const context = setup(t)
  await context.state.generate(); context.state.chosen.value = [0]
  await context.state.apply()
  const after = context.events.at(-1).payload.length
  context.state.open.value = true; context.state.chosen.value = [0]
  await context.state.apply()
  assert.equal(context.events.length, 1, 'already-applied rows are inert')
  assert.equal(effectiveStep(context.props.steps[0]).assertions.length, after)
  // Regenerating offers a fresh list; an identical suggestion is skipped on apply and reported.
  await context.state.generate(); context.state.chosen.value = [0]
  await context.state.apply()
  assert.equal(context.events.length, 1)
  assert.equal(effectiveStep(context.props.steps[0]).assertions.length, after)
  assert.ok(context.messages.some(m => m.text.includes('已存在')))
})

test('repeated generate clicks send one model request while the first is pending', async t => {
  const pending = deferred()
  const context = setup(t, 'AiAssertions', { post: path => path === '/ai/suggest-assertions' ? pending.promise : undefined })
  const first = context.state.generate(), second = context.state.generate()
  const requests = context.calls.filter(call => call.path === '/ai/suggest-assertions')
  pending.resolve(aiResponse(requests[0].payload)); await Promise.all([first, second])
  assert.equal(requests.length, 1)
})

test('regeneration does not lose the accepted count or modified count for prior suggestions', async t => {
  const context = setup(t)
  await context.state.generate(); context.state.chosen.value = [0]; await context.state.apply()
  await context.state.generate()
  context.props.steps[0].overrides.assertions.at(-1).op = 'exists'
  await settle()
  const changes = feedbacks(context).filter(call => call.payload.action === 'modified')
  assert.equal(changes.length, 1)
  assert.equal(changes[0].payload.selected_count, 1)
  assert.equal(changes[0].payload.modified_count, 1)
})

test('closing the accepted generation does not label it dismissed or emit repeated feedback', async t => {
  const context = setup(t)
  await context.state.generate(); context.state.chosen.value = [0]; await context.state.apply()
  context.state.open.value = true; context.state.dismiss(); context.state.dismiss()
  assert.deepEqual(feedbacks(context).map(call => call.payload.action), ['accepted'])
})

test('unmounted suggestion requests cannot populate late content', async t => {
  const pending = deferred()
  const context = setup(t, 'AiAssertions', { post: path => path === '/ai/suggest-assertions' ? pending.promise : undefined })
  const work = context.state.generate(), sent = context.calls.find(call => call.payload?.draft_token)
  context.unmount(); pending.resolve(aiResponse(sent.payload)); await work
  assert.equal(context.state.suggestions.value.length, 0)
  assert.equal(context.events.length, 0)
})

test('unmount during application does not retain undo state or send success feedback', async t => {
  const context = setup(t)
  await context.state.generate(); context.state.chosen.value = [0]
  const pending = context.state.apply()
  context.unmount(); await pending
  assert.equal(context.events.length, 1)
  assert.equal(context.state.applied.value, null)
  assert.equal(feedbacks(context).length, 0)
  assert.equal(context.messages.some(message => message.kind === 'success'), false)
})

test('a different run invalidates cached AI failure explanations', async t => {
  const context = setup(t, 'AiFailureExplanation')
  await context.state.explain()
  context.props.runId = 'run-two'; await settle(); await context.state.explain()
  assert.equal(context.state.data.value.facts[0].step_id, 'run-two')
  assert.equal(context.calls.filter(call => call.path === '/ai/explain-failure').length, 2)
})

test('a late failure explanation never overwrites a newer report explanation', async t => {
  const pending = deferred()
  const context = setup(t, 'AiFailureExplanation', { post: (path, payload) => path === '/ai/explain-failure' && payload.run_id === 'run-one' ? pending.promise : undefined })
  const old = context.state.explain()
  context.props.runId = 'run-two'; await settle(); await context.state.explain()
  pending.resolve(explanation('run-one')); await old
  assert.equal(context.state.data.value.facts[0].step_id, 'run-two')
})

test('repeated explain clicks do not duplicate the same model request', async t => {
  const pending = deferred()
  const context = setup(t, 'AiFailureExplanation', { post: path => path === '/ai/explain-failure' ? pending.promise : undefined })
  const first = context.state.explain(), second = context.state.explain()
  pending.resolve(explanation('run-one')); await Promise.all([first, second])
  assert.equal(context.calls.filter(call => call.path === '/ai/explain-failure').length, 1)
})

test('a failed AI explanation can retry without stale error or duplicated content', async t => {
  let first = true
  const context = setup(t, 'AiFailureExplanation', { post: path => {
    if (path !== '/ai/explain-failure' || !first) return undefined
    first = false; return Promise.reject(new Error('temporary model error'))
  } })
  await context.state.explain()
  assert.equal(context.state.busy.value, false)
  assert.equal(context.state.error.value, 'temporary model error')
  await context.state.explain()
  assert.equal(context.state.error.value, '')
  assert.equal(context.state.data.value.facts[0].step_id, 'run-one')
})
