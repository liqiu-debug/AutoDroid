import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { ref, computed, reactive, nextTick, watch } from 'vue'
import { describeRunSubmission, submissionNotice } from '../src/utils/uiRunPresentation.js'
import { submitExecution, pendingExecution, rememberWebsocketExecution, settlePendingExecution, UNKNOWN_LAUNCH_MESSAGE } from '../src/utils/executionLaunch.js'
import { getErrorDetail } from '../src/utils/errors.js'
import * as actionConstants from '../src/utils/actionConstants.js'
import * as statusMeta from '../src/utils/statusMeta.js'
import * as versionToken from '../src/utils/versionToken.js'
import * as imageOptions from '../src/utils/imageOptions.js'

test.afterEach(() => { for (const kind of ['case','scenario']) for (const id of [9,11,42,73]) settlePendingExecution(kind,id) })
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
  const dependencies = { defineStore: (_name, factory) => factory, ref, computed, nextTick, api, ElMessage: messages(), createUuid: () => `step-${++serial}`, ...versionToken, ...imageOptions }
  const store = new Function(...Object.keys(dependencies), source + '\nreturn useCaseStore()')(...Object.values(dependencies))
  store.newCase(); store.currentCase.value.name = '最新用例'; store.addStep({ action: 'wait', value: '1' })
  return { store, calls, api }
}
function editorSetup(name, extra = {}) {
  const file = name.startsWith('Case') ? `cases/${name}` : `scenarios/${name}`
  const source = stripImports(readFileSync(new URL(`../src/views/${file}.vue`, import.meta.url), 'utf8').match(/<script setup>([\s\S]*?)<\/script>/)[1])
  const connections = [], route = { params: {}, query: {} }, routeWatchers = []
  const dependencies = { describeRunSubmission, submissionNotice, submitExecution, pendingExecution, rememberWebsocketExecution, settlePendingExecution, UNKNOWN_LAUNCH_MESSAGE, getErrorDetail, ...actionConstants, ref, computed, reactive, watch: (source, callback) => { routeWatchers.push({source,callback}) }, onMounted: () => {}, onUnmounted: () => {}, onActivated: () => {}, onDeactivated: () => {},
    useRoute: () => route, useRouter: () => ({ push: () => {}, replace: () => {} }), useUnsavedGuard: () => {},
    useUserStore: () => ({ userInfo: { id: 1, role: 'admin' } }), useClientMode: () => ({ isMobileMode: ref(false) }),
    ElMessage: messages(), ElMessageBox: { confirm: async () => {} }, createUuid: () => 'ui-step',
    setInterval: () => 1, clearInterval: () => {}, setTimeout: () => 1, clearTimeout: () => {},
    // 轮询控制器桩：与 setInterval 桩同策略，测试不起真实定时器
    createPollingLoop: () => ({ start() {}, stop() {}, runNow() {}, isActive: () => false, isInflight: () => false }),
    createGatedTask: fn => fn,
    window: { location: { protocol: 'http:', host: 'test.local' } },
    WebSocket: class { constructor(url) { this.url = url; connections.push(this) } close() { this.onclose?.() } },
    ...versionToken,
    ...extra,
  }
  if (extra.api) Object.assign(extra.api, {
    getExecutionCapabilities: extra.api.getExecutionCapabilities || (async () => ({ clientMode: true, execution_preflight_v1: true, launch_idempotency: true, engine_instance_id: 'engine-a' })),
    getExecutionSessionKey: extra.api.getExecutionSessionKey || (() => 'session-a'),
    ensureCaseExecutionAllowed: extra.api.ensureCaseExecutionAllowed || (async () => {}),
    ensureScenarioExecutionAllowed: extra.api.ensureScenarioExecutionAllowed || (async () => {}),
  })
  if (name.startsWith('Case')) Object.assign(dependencies, statusMeta)
  const exports = name === 'CaseEditor' ? 'handleRun,submitMultiRun,currentCase,envId,deviceStageRef,logConsoleRef,multiRunForm,runBusy,runPhase,isRunning,handleRunStart,handleRunError,pendingLaunch,wsOutcomeUnknown,activeRun,restoreActiveRun,pollActiveRun,handleRunComplete,detachExecutionView'
    : name === 'ScenarioEditor' ? 'saveSteps,runScenario,submitMultiRun,currentScenario,scenarioSteps,envId,multiRunForm,isDirty,scenarioId,runBusy,runPhase,savedSnapshot,pendingLaunch,wsOutcomeUnknown,activeRun,running,openRunDialog,restoreActiveRun,pollActiveRun'
    : `confirmRun,runForm,running${name.replace('List','')}Id,runBusy,runPhase,pendingLaunch,handleRunClick,active${name.replace('List','')}Runs`
  return { state: new Function(...Object.keys(dependencies), source + `\nreturn {${exports}}`)(...Object.values(dependencies)), connections, route, routeWatchers }
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

test('first case run saves latest draft and locks through server preflight until run_start', async () => {
  const wait = deferred()
  const context = caseEditor({ ensureCaseExecutionAllowed: () => wait.promise })
  const run = context.state.handleRun(); await settle()
  assert.equal(context.calls[0][1].name, '最新用例')
  context.state.envId.value = 99; context.state.deviceStageRef.value.selectedSerial = 'serial-b'
  await context.state.handleRun()
  wait.resolve(); await run
  assert.deepEqual(context.prechecks, [], 'new engine performs preflight once')
  assert.deepEqual(context.connections, [[42, 3, 'serial-a']])
  assert.equal(context.state.runPhase.value, 'submitting')
  assert.equal(context.state.runBusy.value, true)
  context.state.handleRunStart({ run_id: 'one', device_serial: 'serial-a' })
  assert.equal(context.state.runBusy.value, false)
  assert.equal(context.state.isRunning.value, true)
})

test('case save failure prevents all prechecks and executions and permits retry', async () => {
  let fail = true
  const context = caseEditor({ replaceCaseStandardSteps: async () => { if (fail) throw new Error('offline'); return { data: undefined } } })
  await context.state.handleRun()
  assert.equal(context.prechecks.length, 0); assert.equal(context.connections.length, 0)
  assert.equal(context.state.runBusy.value, false)
  fail = false; await context.state.handleRun()
  assert.equal(context.prechecks.length, 0); assert.equal(context.connections.length, 1)
  assert.equal(context.calls.filter(c => c[0] === 'create').length, 1)
})

test('case multi-device run saves first and locks submitted devices until batch request settles', async () => {
  const wait = deferred(), batches = []
  const context = caseEditor({ runTestCaseBatch: (...args) => { batches.push(args); return wait.promise } })
  context.state.multiRunForm.value.deviceSerials = ['a', 'b']
  const run = context.state.submitMultiRun(); await settle()
  assert.deepEqual(batches.map(args => args.slice(0, 3)), [[42, 3, ['a', 'b']]])
  assert.equal(context.prechecks.length, 0)
  assert.ok(batches[0][3].requestId)
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
  assert.match(context.connections[0].url, /\/73\?env_id=5&device_serial=device-a$/)
})

test('scenario multi-device save freezes selections and submits once without frontend preflight', async () => {
  const pending = deferred(), requests = []
  const context = scenarioEditor({ runScenario: (...args) => { requests.push(args); return pending.promise } })
  context.state.multiRunForm.value.deviceSerials = ['a', 'b']
  const run = context.state.submitMultiRun(); await settle()
  context.state.envId.value = 50; context.state.multiRunForm.value.deviceSerials = ['c']
  await context.state.submitMultiRun()
  assert.equal(requests.length, 1)
  assert.deepEqual(requests[0].slice(0,3), [73, 5, ['a', 'b']])
  assert.equal(context.calls.filter(c => c[0] === 'precheck').length, 0)
  pending.resolve({ data: { runs: [{ device_serial: 'a' }], blocked_prechecks: [{device_serial:'b',reason:'无可执行步骤'}] } }); await run
  assert.deepEqual(context.state.activeRun.value.device_serials, ['a'])
})

for (const type of ['Case', 'Scenario']) test(`${type} list freezes selections and rejects duplicate in-flight submissions without prechecking`, async () => {
  const pending = deferred(), runs = []
  const run = async (...args) => { runs.push(args); return pending.promise }
  const api = { precheckTestCase: () => assert.fail('duplicate preflight'), precheckScenario: () => assert.fail('duplicate preflight'), runTestCaseBatch: run, runScenario: run }
  const { state } = editorSetup(type+'List', { api })
  state[`running${type}Id`].value = 9; state.runForm.envId = 4; state.runForm.deviceSerials = ['first', 'second']
  const submit = state.confirmRun(); await settle()
  state.runForm.envId = 10; state.runForm.deviceSerials = ['late']; state[`running${type}Id`].value = 11
  await state.confirmRun(); assert.equal(runs.length, 1)
  pending.resolve({ data: { runs: [{ device_serial:'first', queued: true, queue_position:2 }] } }); await submit
  assert.deepEqual(runs[0].slice(0, 3), [9, 4, ['first', 'second']])
  assert.equal(state.runBusy.value, false)
})

for (const type of ['Case', 'Scenario']) test(`${type} list retries an uncertain launch with the same ID and original selections`, async () => {
  const runs = []
  const run = async (...args) => {
    runs.push(args)
    if (runs.length === 1) throw Object.assign(new Error('timeout'), { code: 'ECONNABORTED', request: {} })
    return { data: { runs:[{device_serial:'first',run_id:'one'}] } }
  }
  const { state } = editorSetup(type+'List', { api:{runTestCaseBatch:run,runScenario:run} })
  state[`running${type}Id`].value = 9; state.runForm.envId = 4; state.runForm.deviceSerials = ['first']
  await state.confirmRun()
  assert.equal(state.pendingLaunch.value.retryable, true)
  state.runForm.envId = 12; state.runForm.deviceSerials = ['different']
  await state.confirmRun()
  assert.deepEqual(runs[0], runs[1])
  assert.equal(state.pendingLaunch.value, null)
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

test('old Desktop case websocket keeps the HTTP preflight and its image gate', async () => {
  const context = caseEditor({getExecutionCapabilities:async()=>({clientMode:true})})
  await context.state.handleRun()
  assert.deepEqual(context.prechecks,[[42,3,'serial-a']])
  assert.equal(context.connections.length,1)
})

test('case websocket transport failure remains unresolved and cannot launch again when no active task is visible', async () => {
  const context = caseEditor({getActiveRuns:async()=>({data:{items:[]}})})
  await context.state.handleRun()
  context.state.handleRunError({outcomeUnknown:true})
  assert.equal(context.state.wsOutcomeUnknown.value,true)
  await context.state.handleRun()
  assert.equal(context.connections.length,1)
  assert.equal(context.state.wsOutcomeUnknown.value,true)
})

test('scenario websocket stays submitting until run_start and tracks an accepted task after disconnect', async () => {
  const context = scenarioEditor({getActiveRuns:async()=>({data:{items:[]}})})
  await context.state.runScenario('usb')
  assert.equal(context.state.runPhase.value,'submitting')
  assert.equal(context.connections.length,1)
  const socket = context.connections[0]
  socket.onopen()
  assert.equal(context.state.runPhase.value,'submitting','open is not acceptance')
  socket.onmessage({data:JSON.stringify({type:'run_start',execution_id:7,device_serial:'usb'})})
  assert.equal(context.state.runPhase.value,'')
  socket.onclose()
  assert.equal(context.state.wsOutcomeUnknown.value,false)
  assert.deepEqual(context.state.activeRun.value.execution_ids,[7])
  assert.equal(context.state.activeRun.value.unknown,false)
})

test('LogConsole ignores old socket callbacks and distinguishes transport loss from explicit rejection', () => {
  const source = stripImports(readFileSync(new URL('../src/components/LogConsole.vue',import.meta.url),'utf8').match(/<script setup>([\s\S]*?)<\/script>/)[1])
  const sockets = [], events = []
  const dependencies = {ref,computed,watch:()=>{},nextTick:()=>{},onUnmounted:()=>{},defineProps:()=>({}),defineEmits:()=> (...args)=>events.push(args),defineExpose:()=>{},setTimeout:()=>1,clearTimeout:()=>{},api:{getReportAssetUrl:id=>id},window:{location:{protocol:'http:',host:'test'}},WebSocket:class {constructor(){sockets.push(this)} close(){this.onclose?.()}}}
  const state = new Function(...Object.keys(dependencies),source+'\nreturn {connect,runStatus}')( ...Object.values(dependencies))
  state.connect(1); const first = sockets[0]
  state.connect(2); const second = sockets[1]
  first.onclose(); first.onerror()
  assert.equal(events.length,0)
  second.onopen(); assert.equal(state.runStatus.value,'submitting')
  second.onmessage({data:JSON.stringify({type:'error',message:'预检失败'})})
  assert.equal(events.at(-1)[1].outcomeUnknown,undefined)
  assert.equal(state.runStatus.value,'failed')
  state.connect(3); const third = sockets[2]
  third.onmessage({data:JSON.stringify({type:'run_start'})})
  third.onclose()
  assert.equal(events.at(-1)[1].outcomeUnknown,true)
  assert.equal(state.runStatus.value,'unknown')
})

test('case websocket pending state survives editor recreation and prevents a second execution connection', async () => {
  const first = caseEditor({getActiveRuns:async()=>({data:{items:[]}})})
  await first.state.handleRun()
  assert.equal(first.connections.length,1)
  const second = caseEditor({getActiveRuns:async()=>({data:{items:[]}})})
  second.state.currentCase.value.id = 42
  await second.state.handleRun()
  assert.equal(second.connections.length,0)
  assert.equal(second.state.pendingLaunch.value.transport,'websocket')
  assert.equal(second.state.pendingLaunch.value.retryable,false)
})

test('accepted case websocket main button cancels instead of treating its retained marker as pending', async () => {
  const cancelled = []
  const context = caseEditor({cancelRun:async payload=>{cancelled.push(payload)}})
  await context.state.handleRun()
  context.state.handleRunStart({case_id:42,run_id:'run-one',device_serial:'serial-a'})
  await context.state.handleRun()
  assert.equal(cancelled.length,1)
  assert.deepEqual(cancelled[0].run_ids,['run-one'])
  assert.equal(context.state.isRunning.value,false)
  assert.equal(context.state.pendingLaunch.value,null)
  assert.equal(pendingExecution('case',42),null)
})

for (const type of ['Case','Scenario']) test(`${type} list can cancel a websocket task after cross-page recovery`, async () => {
  const kind = type.toLowerCase(), cancelled = []
  rememberWebsocketExecution(kind,9,['usb'],true)
  const api = {getActiveRuns:async()=>({data:{items:[{run_id:'one',execution_id:1,device_serial:'usb'}]}}),cancelRun:async payload=>{cancelled.push(payload)}}
  const context = editorSetup(type+'List',{api})
  await context.state.handleRunClick({id:9})
  assert.equal(pendingExecution(kind,9),null,'recognized task is owned by active state')
  assert.equal(cancelled.length,0)
  await context.state.handleRunClick({id:9})
  assert.equal(cancelled.length,1)
  assert.equal(cancelled[0].target_id,9)
})

for (const type of ['Case','Scenario']) test(`${type} editor clears accepted websocket local state after restoration finds completion`, async () => {
  const context = type === 'Case' ? caseEditor({getActiveRuns:async()=>({data:{items:[]}})}) : scenarioEditor({getActiveRuns:async()=>({data:{items:[]}})})
  const id = type === 'Case' ? 42 : 73, kind = type.toLowerCase()
  if (type === 'Case') context.state.currentCase.value.id = id
  else await context.state.saveSteps()
  context.state.pendingLaunch.value = rememberWebsocketExecution(kind,id,['usb'],true)
  context.state.wsOutcomeUnknown.value = true
  context.state.runPhase.value = 'submitting'
  context.state.activeRun.value = {run_ids:['one']}
  context.state[type === 'Case' ? 'isRunning' : 'running'].value = true
  await context.state.restoreActiveRun()
  assert.equal(context.state.pendingLaunch.value,null)
  assert.equal(context.state.activeRun.value,null)
  assert.equal(context.state.wsOutcomeUnknown.value,false)
  assert.equal(context.state.runPhase.value,'')
  assert.equal(context.state[type === 'Case' ? 'isRunning' : 'running'].value,false)
})

test('HTTP pending submission permits cancellation of recognized active devices without permitting a new submission', async () => {
  const cancelled = []
  const context = caseEditor({runTestCaseBatch:async()=>{throw Object.assign(new Error('timeout'),{code:'ECONNABORTED',request:{}})},getActiveRuns:async()=>({data:{items:[{run_id:'accepted-a',device_serial:'a'}]}}),cancelRun:async payload=>{cancelled.push(payload)}})
  context.state.multiRunForm.value.deviceSerials = ['a','b']
  await context.state.submitMultiRun()
  const original = context.state.pendingLaunch.value
  await context.state.restoreActiveRun()
  await context.state.handleRun()
  assert.equal(cancelled.length,1)
  assert.deepEqual(cancelled[0].run_ids,['accepted-a'])
  assert.equal(context.state.pendingLaunch.value.requestId,original.requestId)
  assert.equal(pendingExecution('case',42).requestId,original.requestId)
  await context.state.handleRun()
  assert.equal(context.connections.length,0)
})

test('reused case editor rejects old socket events after route identity detaches', async () => {
  const context = caseEditor()
  await context.state.handleRun()
  context.state.handleRunStart({case_id:42,run_id:'old'})
  context.state.detachExecutionView()
  context.state.currentCase.value.id = 43
  const retained = {kind:'case',targetId:43,retryable:true,requestId:'new-pending',devices:['new']}
  context.state.pendingLaunch.value = retained
  context.state.handleRunComplete({case_id:42,success:true})
  context.state.handleRunError({case_id:42,message:'late old error'})
  context.state.handleRunStart({case_id:42,run_id:'late'})
  assert.equal(context.state.pendingLaunch.value.requestId,'new-pending')
  assert.equal(context.state.isRunning.value,false)
  assert.equal(pendingExecution('case',42).accepted,true,'detaching observation preserves original execution')
})
