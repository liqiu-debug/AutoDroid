import test from 'node:test'
import assert from 'node:assert/strict'
import { submitExecution, isLaunchOutcomeUnknown, pendingExecution, settlePendingExecution } from '../src/utils/executionLaunch.js'
import { acceptedDeviceSerials, submissionNotice } from '../src/utils/uiRunPresentation.js'
import { describeError } from '../src/utils/errors.js'

const selection = { kind:'scenario',targetId:12,envId:3,devices:['a','b'] }
const info = {clientMode:true,launch_idempotency:true,engine_instance_id:'engine-a'}
function context(overrides = {}) {
  const calls = []
  const api = {
    getExecutionCapabilities: async () => info,
    getExecutionSessionKey: () => 'session-a',
    runScenario: async (...args) => { calls.push(args); return {data:{runs:[{device_serial:'a'}]}} },
    ...overrides,
  }
  return { api, calls }
}
test.afterEach(() => { for (const id of [12,99]) for (const kind of ['case','scenario']) settlePendingExecution(kind,id) })
const timeout = () => Object.assign(new Error('timeout'), {code:'ECONNABORTED',request:{}})

test('uncertain launch retries the original ID and frozen device/environment snapshot', async () => {
  let saved
  const {api,calls} = context()
  api.runScenario = async (...args) => { calls.push(args); if (calls.length === 1) throw timeout(); return {data:{}} }
  await assert.rejects(() => submitExecution(api,selection), error => { saved = error.launchPending; return true })
  assert.ok(saved.requestId)
  await submitExecution(api,{...selection,envId:99,devices:['c']},saved)
  assert.equal(calls.length,2)
  assert.deepEqual(calls[0],calls[1])
  assert.deepEqual(calls[1].slice(0,3),[12,3,['a','b']])
})

test('old Desktop and Web do not offer unsafe retry after an unknown outcome', async () => {
  for (const capability of [{clientMode:false},{clientMode:true,engine_instance_id:'legacy'}]) {
    settlePendingExecution('scenario',12)
    const {api,calls} = context({getExecutionCapabilities:async()=>capability,runScenario:async(...args)=>{calls.push(args);throw timeout()}})
    let saved
    await assert.rejects(() => submitExecution(api,selection), error => { saved = error.launchPending; return true })
    assert.equal(saved.retryable,false)
    await assert.rejects(() => submitExecution(api,selection,saved), /查看原执行记录/)
    assert.equal(calls.length,1)
  }
})

test('engine restart or session switch removes old retry credentials and blocks resubmission', async () => {
  const base = { ...selection,requestId:'same',engineInstanceId:'engine-a',sessionKey:'session-a',retryable:true }
  for (const override of [{getExecutionCapabilities:async()=>({...info,engine_instance_id:'engine-b'})},{getExecutionSessionKey:()=> 'session-b'}]) {
    const {api,calls} = context(override)
    await assert.rejects(() => submitExecution(api,selection,base), error => {
      assert.equal(error.launchPending.retryable,false)
      assert.equal(error.launchPending.requestId,null)
      assert.equal(error.launchPending.engineInstanceId,null)
      return true
    })
    assert.equal(calls.length,0)
  }
})

test('conflict and unknown-launch replies retain a safe unresolved state', async () => {
  for (const [status,code,retryable] of [[409,'L1002_ENGINE_INSTANCE_CHANGED',false],[409,'L1001_IDEMPOTENCY_CONFLICT',false],[500,'L1004_LAUNCH_RESULT_UNKNOWN',true]]) {
    settlePendingExecution('scenario',12)
    const {api} = context({runScenario:async()=>{throw {response:{status,data:{detail:{code}}}}}})
    await assert.rejects(() => submitExecution(api,selection), error => { assert.equal(error.launchPending.retryable,retryable); return true })
  }
  assert.equal(isLaunchOutcomeUnknown({response:{status:400}}),false)
})

test('failed capability refresh never clears a pending launch or sends another start', async () => {
  const {api,calls} = context({getExecutionCapabilities:async()=>{throw new Error('offline')}})
  const pending = {...selection,retryable:true,requestId:'same'}
  await assert.rejects(() => submitExecution(api,selection,pending), error => error.launchPending === pending)
  assert.equal(calls.length,0)
})

test('submission presentation uses accepted devices and shows specific blocked and deferred details', () => {
  const data = {runs:[{device_serial:'a',cloud_record_pending:true}],blocked_prechecks:[{device_serial:'b',reason:'case precheck failed',precheck:{steps:[{status:'FAIL',message:'缺少变量 token'}]}}]}
  assert.deepEqual(acceptedDeviceSerials(data,['a','b']),['a'])
  const notice = submissionNotice(data,['a','b'])
  assert.equal(notice.warning,true)
  assert.match(notice.text,/已在 1 台/)
  assert.match(notice.text,/云端执行记录尚未确认/)
  assert.match(notice.text,/b - 缺少变量 token/)
})

test('all blocked case and scenario errors become actionable Chinese messages', () => {
  for (const code of ['C1001_CASE_PRECHECK_FAILED','S1001_SCENARIO_PRECHECK_FAILED']) {
    assert.equal(describeError({response:{status:400,data:{detail:{code,message:'case precheck failed for all selected devices',items:[{device_serial:'iphone',precheck:{has_runnable_steps:false}}]}}}}), '运行前预检未通过：iphone - 当前设备无可执行步骤（全部跳过）')
  }
})


test('page recreation recovers the original uncertain launch while another target stays independent', async () => {
  const calls = []
  const first = context({runScenario:async(...args)=>{calls.push(args);throw timeout()}})
  await assert.rejects(() => submitExecution(first.api,selection))
  const retained = pendingExecution('scenario',12)
  assert.ok(retained.requestId)
  assert.equal(pendingExecution('case',12),null)
  const recreated = context({runScenario:async(...args)=>{calls.push(args);return {data:{}}}})
  await submitExecution(recreated.api,{...selection,envId:999,devices:['new-form']})
  assert.deepEqual(calls[0],calls[1])
  assert.equal(pendingExecution('scenario',12),null)
  await submitExecution(recreated.api,{...selection,targetId:99})
  assert.equal(calls[2][0],99)
})

test('session change leaves a credential-free tombstone across recreated pages', async () => {
  const first = context({runScenario:async()=>{throw timeout()}})
  await assert.rejects(() => submitExecution(first.api,selection))
  const changed = context({getExecutionSessionKey:()=> 'changed'})
  await assert.rejects(() => submitExecution(changed.api,selection))
  const retained = pendingExecution('scenario',12)
  assert.equal(retained.retryable,false)
  assert.equal(retained.requestId,null)
  assert.equal(retained.engineInstanceId,null)
  assert.equal(retained.sessionKey,null)
  assert.equal(retained.envId,undefined)
  assert.deepEqual(retained.devices,[])
  await assert.rejects(() => submitExecution(context().api,selection))
})

test('concurrent recreated components share one submission before capability discovery resolves', async () => {
  let releaseCapabilities
  const gate = new Promise(resolve => { releaseCapabilities = resolve })
  const {api,calls} = context({getExecutionCapabilities:() => gate})
  const first = submitExecution(api,selection)
  const second = submitExecution(api,{...selection,envId:999,devices:['changed']})
  assert.equal(first,second,'the single-flight promise is registered before the first await')
  releaseCapabilities(info)
  const [left,right] = await Promise.all([first,second])
  assert.equal(calls.length,1)
  assert.equal(left.launch.requestId,right.launch.requestId)
  assert.deepEqual(calls[0].slice(0,3),[12,3,['a','b']])
})
