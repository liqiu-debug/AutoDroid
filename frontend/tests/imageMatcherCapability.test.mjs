import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = file => readFileSync(new URL(file, import.meta.url), 'utf8')
const stripImports = source => source.replace(/^import [\s\S]*? from ['"][^'"]+['"]\s*\n/gm, '')
const capability = { version: 'strict-v1', validated: true, preview: true, metadata: true }
const upgraded = { clientMode: true, image_matcher: capability }
const upgradeMessage = /请升级桌面客户端以使用高精度图像功能/

function setup({ info = upgraded, status, cases = {}, scenarios = {}, desktop = false } = {}) {
  const calls = []
  const instance = {
    interceptors: { request: { use() {} }, response: { use() {} } },
    get: async path => {
      calls.push(['get', path])
      if (path === '/client/info') {
        if (status) throw Object.assign(new Error('client probe failed'), { response: { status } })
        return { data: info }
      }
      const caseMatch = path.match(/^\/cases\/(\d+)\/steps$/)
      if (caseMatch) return { data: cases[caseMatch[1]] || [] }
      const scenarioMatch = path.match(/^\/scenarios\/(\d+)\/steps$/)
      if (scenarioMatch) return { data: scenarios[scenarioMatch[1]] || [] }
      return { data: {} }
    },
    post: async (path, payload) => { calls.push(['post', path, payload]); return { data: { ok: true } } }
  }
  const source = stripImports(read('../src/api/index.js')).replace('export default', 'const defaultApi =').replace(/export const/g, 'const')
  const deps = { axios: { create: () => instance }, window: desktop ? { desktop: {} } : {}, normalizeError() {} }
  const api = new Function(...Object.keys(deps), source + '\nreturn defaultApi')(...Object.values(deps))
  return { api, calls, instance }
}

test('old Desktop blocks recording, preview and all strict image single steps before forwarding requests', async () => {
  const { api, calls } = setup({ info: { clientMode: true, version: '1.0.9' } })
  for (const request of [() => api.cropTemplate('FRAME', 0, 0, 40, 40), () => api.matchImage({ image_path: 'x.png' }),
    () => api.executeStep({ step: { action: 'click_image' } }), () => api.executeStep({ step: { action: 'assert_image' } }), () => api.ensureImageMatcher()]) {
    await assert.rejects(request, upgradeMessage)
  }
  assert.deepEqual(calls, [['get', '/client/info']], 'capability probe is cached, no cloud preview or local execution is sent')
  await api.executeStep({ step: { action: 'click', selector: 'ok' } })
  assert.equal(calls.at(-1)[1], '/device/execute_step', 'semantic non-image actions remain available')
})

test('new Desktop capability allows image calls after one cached local probe', async () => {
  const { api, calls } = setup()
  await Promise.all([api.matchImage({ image_path: 'x.png' }), api.cropTemplate('FRAME', 0, 0, 40, 40)])
  await api.executeStep({ step: { action: 'click_image' } })
  assert.equal(calls.filter(call => call[1] === '/client/info').length, 1)
  assert.deepEqual(calls.filter(call => call[0] === 'post').map(call => call[1]).sort(), ['/device/crop_template', '/device/execute_step', '/device/match_image'])
})

test('Web 404 permits server execution while an Electron bridge with missing capability is blocked', async () => {
  const web = setup({ status: 404 })
  await web.api.matchImage({ image_path: 'x.png' })
  assert.equal(web.calls.at(-1)[1], '/device/match_image')
  const desktop = setup({ status: 404, desktop: true })
  await assert.rejects(() => desktop.api.matchImage({ image_path: 'x.png' }), upgradeMessage)
  assert.equal(desktop.calls.some(call => call[0] === 'post'), false)
})

test('failed capability discovery cannot silently assume Web or remain permanently cached', async () => {
  const context = setup({ status: 502 })
  await assert.rejects(() => context.api.matchImage({}), /无法确认图像匹配能力/)
  await assert.rejects(() => context.api.matchImage({}), /无法确认图像匹配能力/)
  assert.equal(context.calls.length, 2)
})

test('old Desktop image cases are blocked through every case start and precheck entry', async () => {
  const context = setup({ info: { clientMode: true }, cases: { 1: [{ action: 'click_image' }], 2: [{ action: 'click' }], 3: [{ action: 'assert_image', args: { match_mode: 'not_exists' } }] } })
  for (const request of [() => context.api.runTestCase(1), () => context.api.runTestCaseAsync(1),
    () => context.api.runTestCaseBatch(1, null, ['usb']), () => context.api.precheckTestCase(1, null, 'usb')]) {
    await assert.rejects(request, upgradeMessage)
  }
  assert.equal(context.calls.some(call => call[0] === 'post'), false)
  await assert.rejects(() => context.api.runTestCaseBatch(3, null, ['usb']), upgradeMessage)
  await assert.rejects(() => context.api.precheckTestCase(3, null, 'usb'), upgradeMessage)
  await context.api.runTestCaseBatch(2, null, ['usb'])
  assert.equal(context.calls.at(-1)[1], '/cases/2/run-batch')
})

test('old Desktop scenarios inspect referenced cases before run or websocket precheck', async () => {
  const context = setup({ info: { clientMode: true }, cases: { 1: [{ action: 'click_image' }], 2: [{ action: 'click' }], 3: [{ action: 'assert_image' }] }, scenarios: { 10: [{ case_id: 2 }, { case_id: 1 }], 20: [{ case_id: 2 }], 30: [{ case_id: 3 }] } })
  await assert.rejects(() => context.api.runScenario(10, null, ['usb']), upgradeMessage)
  await assert.rejects(() => context.api.precheckScenario(10, null, 'usb'), upgradeMessage)
  await assert.rejects(() => context.api.runScenario(30, null, ['usb']), upgradeMessage)
  await assert.rejects(() => context.api.precheckScenario(30, null, 'usb'), upgradeMessage)
  assert.equal(context.calls.some(call => call[0] === 'post'), false)
  await context.api.runScenario(20, null, ['usb'])
  assert.equal(context.calls.at(-1)[1], '/scenarios/20/run')
})

test('partial capability declarations cannot silently enable image matching', async () => {
  for (const image_matcher of [{ ...capability, metadata: false }, { ...capability, preview: false }, { ...capability, validated: false }, { ...capability, version: 'legacy' }]) {
    const context = setup({ info: { clientMode: true, image_matcher } })
    await assert.rejects(() => context.api.ensureImageMatcher(), upgradeMessage)
  }
})

test('execution capabilities share the cached image probe and require explicit preflight support', async () => {
  const context = setup({ info: { ...upgraded,execution_preflight_v1:true,launch_idempotency:true,engine_instance_id:'engine-a' } })
  await context.api.ensureCaseExecutionAllowed(1)
  await context.api.ensureScenarioExecutionAllowed(10)
  assert.equal((await context.api.getExecutionCapabilities()).execution_preflight_v1,true)
  assert.equal(context.calls.filter(call => call[1] === '/client/info').length,1)
  const legacy = setup({info:{...upgraded}})
  assert.notEqual((await legacy.api.getExecutionCapabilities()).execution_preflight_v1,true)
})

test('all HTTP launch APIs explicitly set sixty seconds and carry supported idempotency headers', async () => {
  const context = setup({ info:{...upgraded,launch_idempotency:true,engine_instance_id:'engine-a'} })
  const posted = []
  context.instance.post = async (...args) => { posted.push(args); return {data:{}} }
  const launch = {requestId:'original',engineInstanceId:'engine-a'}
  await context.api.runTestCase(1,launch)
  await context.api.runTestCaseAsync(1,2,'usb',launch)
  await context.api.runTestCaseBatch(1,2,['usb'],launch)
  await context.api.runScenario(2,2,['usb'],launch)
  assert.equal(posted.length,4)
  for (const [,,config] of posted) {
    assert.equal(config.timeout,60000)
    assert.deepEqual(config.headers,{'Idempotency-Key':'original','X-AutoDroid-Engine-Instance':'engine-a'})
  }
  await context.api.runScenario(2,2,['usb'])
  assert.equal(posted.at(-1)[2].timeout,60000)
  assert.equal(posted.at(-1)[2].headers,undefined)
})

test('new launch credentials cannot silently fall back to a legacy or changed engine', async () => {
  for (const info of [upgraded,{...upgraded,launch_idempotency:true,engine_instance_id:'engine-b'}]) {
    const context = setup({info})
    await assert.rejects(() => context.api.runScenario(1,null,['usb'],{requestId:'old',engineInstanceId:'engine-a'}),/桌面引擎已变化/)
    assert.equal(context.calls.some(call=>call[0]==='post'),false)
  }
})
