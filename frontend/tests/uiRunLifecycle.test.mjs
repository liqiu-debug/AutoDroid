import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { ref, computed, reactive, nextTick, watch } from 'vue'
import { describeRunSubmission } from '../src/utils/uiRunPresentation.js'
import * as actionConstants from '../src/utils/actionConstants.js'
import * as statusMeta from '../src/utils/statusMeta.js'
import * as versionToken from '../src/utils/versionToken.js'

const copy = value => JSON.parse(JSON.stringify(value))
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const settle = async () => { await nextTick(); for (let i = 0; i < 20; i++) await Promise.resolve() }
const stripImports = source => source.replace(/^import [\s\S]*? from ['"][^'"]+['"]\s*\n/gm, '')
const messages = () => Object.fromEntries(['success', 'warning', 'error', 'info'].map(kind => [kind, () => {}]))
function storeSetup(overrides = {}) {
  const calls = []
  const api = {
    createTestCase: async payload => { calls.push(['create', copy(payload)]); return { data: { ...copy(payload), id: 42 } } },
    updateTestCase: async (id, payload) => { calls.push(['update', id, copy(payload)]); return { data: { ...copy(payload), id } } },
    replaceCaseStandardSteps: async (id, steps) => { calls.push(['steps', id, copy(steps)]); return { data: undefined } },
    getTestCases: async () => ({ data: { items: [] } }),
    ...overrides,
  }
  const source = stripImports(readFileSync(new URL('../src/stores/useCaseStore.js', import.meta.url), 'utf8')).replace('export const useCaseStore', 'const useCaseStore')
  let serial = 0
  const dependencies = { defineStore: (_name, factory) => factory, ref, computed, nextTick, api, ElMessage: messages(), createUuid: () => `step-${++serial}`, ...versionToken }
  const store = new Function(...Object.keys(dependencies), source + '\nreturn useCaseStore()')(...Object.values(dependencies))
  store.newCase(); store.currentCase.value.name = '最新用例'; store.addStep({ action: 'wait', value: '1' })
  return { store, calls, api }
}
function editorSetup(name, extra = {}) {
  const file = name.startsWith('Case') ? `cases/${name}` : `scenarios/${name}`
  const source = stripImports(readFileSync(new URL(`../src/views/${file}.vue`, import.meta.url), 'utf8').match(/<script setup>([\s\S]*?)<\/script>/)[1])
  const connections = [], route = { params: {}, query: {} }
  const dependencies = { describeRunSubmission, ...actionConstants, ref, computed, reactive, watch: () => {}, onMounted: () => {}, onUnmounted: () => {}, onActivated: () => {}, onDeactivated: () => {},
    useRoute: () => route, useRouter: () => ({ push: () => {}, replace: () => {} }), useUnsavedGuard: () => {},
    useUserStore: () => ({ userInfo: { id: 1, role: 'admin' } }), useClientMode: () => ({ isMobileMode: ref(false) }),
    ElMessage: messages(), ElMessageBox: { confirm: async () => {} }, createUuid: () => 'ui-step',
    setInterval: () => 1, clearInterval: () => {}, window: { location: { protocol: 'http:', host: 'test.local' } },
    WebSocket: class { constructor(url) { connections.push(url) } close() {} },
    ...versionToken,
    ...extra,
  }
  if (name.startsWith('Case')) Object.assign(dependencies, statusMeta)
  const exports = name === 'CaseEditor' ? 'handleRun,submitMultiRun,currentCase,envId,deviceStageRef,logConsoleRef,multiRunForm,runBusy,runPhase,isRunning'
    : name === 'ScenarioEditor' ? 'saveSteps,runScenario,submitMultiRun,currentScenario,scenarioSteps,envId,multiRunForm,isDirty,scenarioId,runBusy,runPhase,savedSnapshot'
    : `confirmRun,runForm,running${name.replace('List','')}Id,runBusy,runPhase`
  return { state: new Function(...Object.keys(dependencies), source + `\nreturn {${exports}}`)(...Object.values(dependencies)), connections }
}

test('case save reports partial standard-step failure and preserves ID/draft for retry', async () => {
  let attempts = 0
  const context = storeSetup({ replaceCaseStandardSteps: async () => { if (++attempts === 1) throw new Error('steps offline'); return { data: undefined } } })
  const draft = copy(context.store.currentCase.value.steps)
  assert.equal(await context.store.saveCase(), false)
  assert.equal(context.store.currentCase.value.id, 42)
  assert.deepEqual(copy(context.store.currentCase.value.steps), draft)
  assert.equal(context.store.hasUnsavedChanges.value, true)
  assert.equal(await context.store.saveCase(), true)
  assert.equal(context.calls.filter(c => c[0] === 'create').length, 1)
  assert.equal(context.calls.filter(c => c[0] === 'update').length, 1)
  assert.equal(context.store.hasUnsavedChanges.value, false)
})

test('case save rejects reentrant saves and does not overwrite edits made in flight', async () => {
  const pending = deferred()
  const { store } = storeSetup({ replaceCaseStandardSteps: () => pending.promise })
  const saving = store.saveCase(); await settle()
  assert.equal(await store.saveCase(), false)
  store.currentCase.value.name = '保存期间继续编辑'
  pending.resolve({ data: undefined }); assert.equal(await saving, true)
  assert.equal(store.currentCase.value.name, '保存期间继续编辑')
  assert.equal(store.hasUnsavedChanges.value, true)
  assert.equal(store.saving.value, false)
})

function caseEditor(overrides = {}) {
  const context = storeSetup(overrides)
  const prechecks = [], batches = [], connections = []
  Object.assign(context.api, { precheckTestCase: async (...args) => { prechecks.push(args); return { data: { ok: true } } }, runTestCaseBatch: async (...args) => { batches.push(args); return { data: { batch_id: 7, run_ids: [8] } } }, ...overrides })
  const { state } = editorSetup('CaseEditor', { useCaseStore: () => context.store, storeToRefs: store => store, api: context.api })
  state.envId.value = 3; state.deviceStageRef.value = { selectedSerial: 'serial-a' }
  state.logConsoleRef.value = { connect: (...args) => connections.push(args) }
  return { ...context, state, prechecks, batches, connections }
}

test('first case run saves latest draft before precheck and freezes environment/device throughout', async () => {
  const wait = deferred(), prechecks = []
  const context = caseEditor({ precheckTestCase: (...args) => { prechecks.push(args); return wait.promise } })
  const run = context.state.handleRun(); await settle()
  assert.deepEqual(prechecks, [[42, 3, 'serial-a']])
  assert.equal(context.calls[0][1].name, '最新用例')
  assert.equal(context.state.runPhase.value, 'prechecking')
  context.state.envId.value = 99; context.state.deviceStageRef.value.selectedSerial = 'serial-b'
  await context.state.handleRun()
  assert.equal(prechecks.length, 1)
  wait.resolve({ data: { ok: true } }); await run
  assert.deepEqual(context.connections, [[42, 3, 'serial-a']])
  assert.equal(context.state.runBusy.value, false)
})

test('case save failure prevents all prechecks and executions and permits retry', async () => {
  let fail = true
  const context = caseEditor({ replaceCaseStandardSteps: async () => { if (fail) throw new Error('offline'); return { data: undefined } } })
  await context.state.handleRun()
  assert.equal(context.prechecks.length, 0); assert.equal(context.connections.length, 0)
  assert.equal(context.state.runBusy.value, false)
  fail = false; await context.state.handleRun()
  assert.equal(context.prechecks.length, 1); assert.equal(context.connections.length, 1)
  assert.equal(context.calls.filter(c => c[0] === 'create').length, 1)
})

test('case multi-device run saves first and locks submitted devices until batch request settles', async () => {
  const wait = deferred(), batches = []
  const context = caseEditor({ runTestCaseBatch: (...args) => { batches.push(args); return wait.promise } })
  context.state.multiRunForm.value.deviceSerials = ['a', 'b']
  const run = context.state.submitMultiRun(); await settle()
  assert.deepEqual(batches, [[42, 3, ['a', 'b']]])
  context.state.multiRunForm.value.deviceSerials = ['c']; context.state.envId.value = 8
  await context.state.submitMultiRun()
  assert.equal(batches.length, 1)
  wait.resolve({ data: { batch_id: 7, run_ids: [8, 9] } }); await run
  assert.equal(context.state.runBusy.value, false)
})

function scenarioEditor(overrides = {}) {
  const calls = []
  const api = { createScenario: async payload => { calls.push(['create', copy(payload)]); return { data: { id: 73 } } },
    updateScenario: async (...args) => { calls.push(['update', ...args]) }, updateScenarioSteps: async (...args) => { calls.push(['steps', ...copy(args)]) },
    precheckScenario: async (...args) => { calls.push(['precheck', ...args]); return { data: { ok: true } } },
    runScenario: async (...args) => { calls.push(['run', ...copy(args)]); return { data: { execution_ids: [1], runs: [] } } }, ...overrides }
  const context = editorSetup('ScenarioEditor', { api })
  context.state.currentScenario.value = { name: '场景草稿' }; context.state.scenarioSteps.value = [{ id: 2, name: '用例', alias: '新别名' }]
  context.state.savedSnapshot.value = 'unsaved'; context.state.envId.value = 5
  return { ...context, calls }
}

test('scenario first save retains created ID after partial failure and does not run until retry succeeds', async () => {
  let fail = true
  const context = scenarioEditor({ updateScenarioSteps: async () => { if (fail) throw new Error('steps offline') } })
  assert.equal(await context.state.runScenario('device-a'), false)
  assert.equal(context.state.scenarioId.value, 73)
  assert.equal(context.state.isDirty.value, true)
  assert.equal(context.connections.length, 0)
  assert.equal(context.calls.filter(c => c[0] === 'precheck').length, 0)
  fail = false; assert.equal(await context.state.runScenario('device-a'), true)
  assert.equal(context.calls.filter(c => c[0] === 'create').length, 1)
  assert.equal(context.calls.filter(c => c[0] === 'update').length, 1)
  assert.match(context.connections[0], /\/73\?env_id=5&device_serial=device-a$/)
})

test('scenario multi-device save and submission freezes environment and rejects duplicate submit', async () => {
  const pending = deferred(), prechecks = []
  const context = scenarioEditor({ precheckScenario: (...args) => { prechecks.push(args); return prechecks.length === 1 ? pending.promise : Promise.resolve({ data: { ok: true } }) } })
  context.state.multiRunForm.value.deviceSerials = ['a', 'b']
  const run = context.state.submitMultiRun(); await settle()
  context.state.envId.value = 50; context.state.multiRunForm.value.deviceSerials = ['c']
  await context.state.submitMultiRun()
  assert.equal(prechecks.length, 1)
  pending.resolve({ data: { ok: true } }); await run
  assert.deepEqual(prechecks, [[73, 5, 'a'], [73, 5, 'b']])
  assert.deepEqual(context.calls.find(c => c[0] === 'run'), ['run', 73, 5, ['a', 'b']])
})

for (const type of ['Case', 'Scenario']) test(`${type} list rejects repeated run and uses selection captured before precheck`, async () => {
  const pending = deferred(), prechecks = [], runs = []
  const check = (...args) => { prechecks.push(args); return prechecks.length === 1 ? pending.promise : Promise.resolve({ data: { ok: true } }) }
  const run = async (...args) => { runs.push(args); return { data: { runs: [{ queued: true, queue_position: 2 }], run_ids: [2], execution_ids: [2] } } }
  const api = { precheckTestCase: check, precheckScenario: check, runTestCaseBatch: run, runScenario: run, getScenarios: async () => ({ data: { items: [], total: 0 } }) }
  const { state } = editorSetup(type+'List', { api })
  state[`running${type}Id`].value = 9; state.runForm.envId = 4; state.runForm.deviceSerials = ['first', 'second']
  const submit = state.confirmRun(); await settle()
  state.runForm.envId = 10; state.runForm.deviceSerials = ['late']; state[`running${type}Id`].value = 11
  await state.confirmRun(); assert.equal(prechecks.length, 1)
  pending.resolve({ data: { ok: true } }); await submit
  assert.deepEqual(prechecks, [[9, 4, 'first'], [9, 4, 'second']])
  assert.deepEqual(runs, [[9, 4, ['first', 'second']]])
  assert.equal(state.runBusy.value, false)
})

 test('queued batch messages preserve the queue position instead of claiming all devices started', () => {
  assert.equal(describeRunSubmission({runs:[{queued:true,queue_position:4},{queued:true,queue_position:2}]},2), '已加入执行队列：2 台（最前第 2 位）')
  assert.equal(describeRunSubmission({runs:[{queued:false},{queued:true,queue_position:3}]},2), '已开始 1 台；1 台排队中（最前第 3 位）')
  assert.equal(describeRunSubmission({}, 2), '已在 2 台设备开始执行')
})

test('initial case load snapshots after editor normalization so it is not immediately dirty', async () => {
  const { store } = storeSetup({ getTestCase: async () => ({data:{id:17,name:'已保存用例',variables:[],steps:[{action:'assert_text',value:'欢迎'}]}}), getCaseStandardSteps: async () => ({data:[]}) })
  const stop = watch(() => store.currentCase.value.steps, steps => {
    for (const step of steps) { if (!step.options?.match_mode) step.options = {...step.options,match_mode:'contains'} }
  }, {deep:true})
  try { await store.loadCase(17); assert.equal(store.currentCase.value.steps[0].options.match_mode,'contains'); assert.equal(store.hasUnsavedChanges.value,false) } finally {stop()}
})

test('a partial first save of an unchanged new draft is still visibly unsaved', async () => {
  const { store } = storeSetup({ replaceCaseStandardSteps: async () => { throw new Error('steps unavailable') } })
  store.newCase()
  assert.equal(store.hasUnsavedChanges.value,false)
  assert.equal(await store.saveCase(),false)
  assert.equal(store.currentCase.value.id,42)
  assert.equal(store.hasUnsavedChanges.value,true)
})
