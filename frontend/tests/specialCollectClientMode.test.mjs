import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, reactive, ref, watch, nextTick } from 'vue'
import * as startupUtils from '../src/utils/startup.js'

// 流畅度仍保持旧版门控；启动专项按协议能力开放本机与 Web 派发。
// 覆盖旧桌面禁止代理误执行、平台边界、幂等提交与取消。
// 与仓库既有测试一致，用 node:test + 依赖注入执行 <script setup>（非 vitest）。

const stripImports = source => source.replace(/^import [\s\S]*? from ['"][^'"]+['"]\s*\n/gm, '')
const vueSource = name => readFileSync(new URL(`../src/views/${name}`, import.meta.url), 'utf8')
const scriptOf = name => stripImports(vueSource(name).match(/<script setup>([\s\S]*?)<\/script>/)[1])

const messages = record => Object.fromEntries(
  ['success', 'warning', 'error', 'info'].map(kind => [kind, (...args) => record.push([kind, ...args])])
)

function collectSetup(name, { clientMode = false, exports, startupDevices, startupProtocol = 1, runStartup, startupDeviceError, localStartupResults = [] } = {}) {
  const apiCalls = []
  const notices = []
  const mounted = []
  const supportedDevices = startupDevices || [{ serial: 'USB1', platform: 'android', executor_kind: clientMode ? 'desktop' : 'server', available: true, capabilities: { protocol_version: 1, platforms: ['android'], metrics: ['total_time_ms', 'automation_ms', 'ready_ms'], ready_locators: ['text', 'resource_id', 'description', 'xpath'], trace: true, web_dispatch: true } }]
  const api = {
    getFastbotDevices: async () => { apiCalls.push(['getFastbotDevices']); return { data: [] } },
    getFluencySessions: async () => { apiCalls.push(['getFluencySessions']); return { data: [] } },
    getClientInfo: async () => ({ data: { clientMode, startup: startupProtocol === null ? undefined : { protocol_version: startupProtocol } } }),
    getStartupLocalResults: async () => { apiCalls.push(['getStartupLocalResults']); return { data: { items: localStartupResults } } },
    getStartupDevices: async () => { apiCalls.push(['getStartupDevices']); if (startupDeviceError) throw startupDeviceError; return { data: { items: supportedDevices } } },
    getDeviceList: async () => ({ data: supportedDevices }),
    stopStartupTask: async id => { apiCalls.push(['stopStartupTask', id]); return { data: { id, cancel_requested: true } } },
    getStartupTasks: async () => { apiCalls.push(['getStartupTasks']); return { data: { items: [], total: 0 } } },
    startFluencySession: async (payload) => { apiCalls.push(['startFluencySession', payload]); return { data: { task_id: 1, status: 'RUNNING', markers: [] } } },
    addFluencyMarker: async (taskId, payload) => { apiCalls.push(['addFluencyMarker', taskId, payload]); return { data: { task_id: taskId, status: 'RUNNING', markers: [] } } },
    stopFluencySession: async (taskId) => { apiCalls.push(['stopFluencySession', taskId]); return { data: { task_id: taskId, status: 'COMPLETED', markers: [] } } },
    runStartupTest: async (payload) => { apiCalls.push(['runStartupTest', payload]); return runStartup ? runStartup(payload) : { data: [{ id: 7 }] } },
  }
  const isClientApp = ref(!!clientMode)
  const sessionKey = `session-${Math.random()}`
  api.getExecutionSessionKey = () => sessionKey
  const detectClientApp = async () => { apiCalls.push(['detectClientApp']); return isClientApp.value }
  const deps = {
    computed, reactive, ref, watch,
    ...startupUtils,
    ...(name.includes('Startup') ? { statusTagType: () => 'info' } : {}), runStatusLabel: value => value,
    nextTick: async () => {},
    onMounted: fn => mounted.push(fn),
    onActivated: () => {}, onDeactivated: () => {}, onUnmounted: () => {},
    useRouter: () => ({ push: () => {} }),
    ElMessage: messages(notices),
    api,
    useClientApp: () => ({ isClientApp, detectClientApp }),
    dayjs: () => ({ format: () => 'ts' }),
    runStatusTagType: () => 'info',
    setInterval: () => 1,
    clearInterval: () => {},
    // 轮询控制器桩：与 setInterval 桩同策略，测试不起真实定时器
    createPollingLoop: () => ({ start() {}, stop() {}, runNow() {}, isActive: () => false, isInflight: () => false }),
  }
  const state = new Function(...Object.keys(deps), scriptOf(name) + `\nreturn {${exports}}`)(...Object.values(deps))
  return { state, apiCalls, notices, mounted, isClientApp }
}

test('client mode gates fluency recording with the degradation notice and zero writes', async () => {
  const exports = 'startSession,stopSession,submitMarker,form,sessions,isClientApp'
  const { state, apiCalls, notices } = collectSetup('special/Fluency.vue', { clientMode: true, exports })

  state.form.package_name = 'com.example.app'
  state.form.device_serial = 'USB1'
  await state.startSession()
  assert.deepEqual(notices, [['info', '本地化采集后续版本提供']])
  assert.ok(!apiCalls.some(([name]) => name === 'startFluencySession'), 'no fluency start behind the dead entry')

  state.sessions.value = [{ task_id: 9, status: 'RUNNING', markers: [] }]
  await state.stopSession()
  await state.submitMarker('详情页')
  assert.deepEqual(notices, [
    ['info', '本地化采集后续版本提供'],
    ['info', '本地化采集后续版本提供'],
    ['info', '本地化采集后续版本提供'],
  ])
  assert.ok(!apiCalls.some(([name]) => name === 'stopFluencySession' || name === 'addFluencyMarker'), 'marker/stop writes are gated too')
})

test('web mode keeps the fluency flow unchanged', async () => {
  const exports = 'startSession,form,isClientApp'
  const { state, apiCalls, notices } = collectSetup('special/Fluency.vue', { clientMode: false, exports })

  state.form.package_name = 'com.example.app'
  state.form.device_serial = 'USB1'
  await state.startSession()
  assert.deepEqual(notices, [['success', '已开始录制，请到手机上手动操作目标页面']])
  assert.ok(apiCalls.some(([name]) => name === 'startFluencySession'))
})

test('capable desktop and web nodes submit the same startup contract', async () => {
  for (const clientMode of [true, false]) {
    const { state, apiCalls, notices } = collectSetup('special/Startup.vue', { clientMode, exports: 'submit,form,selectedDevices,fetchDevices' })
    await state.fetchDevices()
    state.form.package_name = 'com.example.app'
    await state.submit()
    const payload = apiCalls.find(([name]) => name === 'runStartupTest')?.[1]
    assert.equal(payload.platform, 'android')
    assert.deepEqual(payload.device_serials, ['USB1'])
    assert.ok(payload.request_id)
    assert.deepEqual(payload.thresholds, { metric: 'total_time_ms', cold_ms: 5000, hot_ms: 1500 })
    assert.equal(payload.perfetto_slow_trace.enabled, false)
    assert.ok(notices.some(([kind]) => kind === 'success'))
  }
})

test('startup acknowledges desktop task envelopes and reports pre-execution failures without claiming a running task', async () => {
  for (const status of ['QUEUED', 'FAILED', 'ABORTED']) {
    const { state, notices } = collectSetup('special/Startup.vue', {
      clientMode: true, exports: 'submit,fetchDevices,form,uncertainSubmission',
      runStartup: async () => ({ data: { tasks: [{ id: 42, status }] } }),
    })
    await state.fetchDevices()
    state.form.package_name = 'com.example.app'
    await state.submit()
    assert.equal(state.uncertainSubmission.value, false)
    assert.ok(notices.some(([kind]) => kind === (status === 'QUEUED' ? 'success' : 'warning')))
    if (status !== 'QUEUED') assert.equal(notices.some(([kind]) => kind === 'success'), false)
  }
})

test('unconfirmed or malformed startup acknowledgements retain the original submission for retry', async () => {
  for (const data of [null, {}, [], [null], [{ id: null }], [{ id: 0 }], [{ id: -1 }], [{ id: '42' }]]) {
    let attempt = 0
    const { state, apiCalls, notices } = collectSetup('special/Startup.vue', {
      exports: 'submit,fetchDevices,form,uncertainSubmission',
      runStartup: async () => attempt++ === 0 ? { data } : { data: { tasks: [{ id: 42 }] } },
    })
    await state.fetchDevices()
    state.form.package_name = 'com.example.app'
    await state.submit()
    assert.equal(state.uncertainSubmission.value, true, JSON.stringify(data))
    assert.equal(notices.some(([kind]) => kind === 'success'), false)
    state.form.package_name = 'changed.after.timeout'
    await state.submit()
    const posts = apiCalls.filter(([name]) => name === 'runStartupTest')
    assert.deepEqual(posts[0][1], posts[1][1])
    assert.equal(state.uncertainSubmission.value, false)
  }
})

test('desktop launch-result-unknown 503 retries the same request and immutable device payload', async () => {
  let attempt = 0
  const { state, apiCalls } = collectSetup('special/Startup.vue', {
    clientMode: true, exports: 'submit,fetchDevices,form,uncertainSubmission',
    runStartup: async () => {
      if (attempt++ === 0) throw { response: { status: 503, data: { detail: { code: 'L1004_LAUNCH_RESULT_UNKNOWN', tasks: [{ id: 42 }] } } } }
      return { data: { tasks: [{ id: 42, status: 'COMPLETED' }] } }
    },
  })
  await state.fetchDevices()
  state.form.package_name = 'com.example.app'
  await state.submit()
  assert.equal(state.uncertainSubmission.value, true)
  await state.submit()
  const posts = apiCalls.filter(([name]) => name === 'runStartupTest')
  assert.deepEqual(posts[0][1], posts[1][1])
  assert.equal(state.uncertainSubmission.value, false)
})

test('old desktop engines cannot fall through to cloud startup routes', async () => {
  const { state, apiCalls, notices } = collectSetup('special/Startup.vue', { clientMode: true, startupProtocol: null, exports: 'submit,fetchDevices,capabilityError,form' })
  await state.fetchDevices()
  state.form.package_name = 'com.example.app'
  await state.submit()
  assert.match(state.capabilityError.value, /升级客户端/)
  assert.ok(!apiCalls.some(([name]) => ['getStartupDevices', 'runStartupTest'].includes(name)))
  assert.ok(notices.some(([kind]) => kind === 'warning'))
})

test('desktop outbox remains visible when cloud device discovery is offline', async () => {
  const context = collectSetup('special/Startup.vue', {
    clientMode: true, startupDeviceError: new Error('offline'),
    localStartupResults: [{ id: 42, run_id: 'local-42', status: 'ABORTED', sync_state: 'pending', summary: { completed_count: 2 } }],
    exports: 'fetchDevices,fetchLocalTasks,capabilitiesReady,localStartupSupported,tasks,tasksTotal',
  })
  await context.state.fetchDevices()
  assert.equal(context.state.capabilitiesReady.value, false)
  assert.equal(context.state.localStartupSupported.value, true)
  await context.state.fetchLocalTasks()
  assert.equal(context.state.tasks.value[0].id, 42)
  assert.equal(context.state.tasks.value[0].sync_state, 'pending')
  assert.equal(context.state.tasksTotal.value, 1)
  assert.ok(context.apiCalls.some(([name]) => name === 'getStartupLocalResults'))

  const old = collectSetup('special/Startup.vue', { clientMode: true, startupProtocol: null, exports: 'fetchDevices,fetchLocalTasks' })
  await old.state.fetchDevices()
  await old.state.fetchLocalTasks()
  assert.equal(old.apiCalls.some(([name]) => name === 'getStartupLocalResults'), false, 'never proxy local outbox reads through an old engine')
})

test('iOS configuration sends Bundle ID and WDA thresholds, without Activity or Perfetto', async () => {
  const startupDevices = [{ serial: 'IOS1', platform: 'ios', executor_kind: 'desktop', available: true, capabilities: { protocol_version: 1, platforms: ['ios'], metrics: ['automation_ms', 'ready_ms'], ready_locators: ['accessibility_id', 'predicate'], trace: false, web_dispatch: true } }]
  const { state, apiCalls } = collectSetup('special/Startup.vue', { startupDevices, exports: 'submit,fetchDevices,form,selectedDevices' })
  state.form.platform = 'ios'
  await nextTick()
  await state.fetchDevices()
  state.form.package_name = 'com.example.ios'
  state.form.activity_name = 'leftover.AndroidActivity'
  state.form.perfetto_enabled = true
  await state.submit()
  const payload = apiCalls.find(([name]) => name === 'runStartupTest')?.[1]
  assert.equal(payload.platform, 'ios')
  assert.equal(payload.package_name, 'com.example.ios')
  assert.equal(payload.activity_name, null)
  assert.equal(payload.thresholds.metric, 'automation_ms')
  assert.equal(payload.perfetto_slow_trace.enabled, false)
})

test('mixed-platform and unavailable selections are rejected before writes', async () => {
  const { state, apiCalls } = collectSetup('special/Startup.vue', { exports: 'submit,fetchDevices,form,selectedDevices,devices' })
  await state.fetchDevices()
  state.form.package_name = 'com.example.app'
  state.devices.value.push({ serial: 'IOS1', platform: 'ios', available: true })
  state.selectedDevices.value = ['USB1', 'IOS1']
  await state.submit()
  assert.ok(!apiCalls.some(([name]) => name === 'runStartupTest'))
})

test('ambiguous startup submission retries its immutable payload and request id', async () => {
  let attempt = 0
  const { state, apiCalls } = collectSetup('special/Startup.vue', { exports: 'submit,fetchDevices,form,uncertainSubmission', runStartup: async () => {
    if (attempt++ === 0) throw new Error('timeout')
    return { data: [{ id: 7 }] }
  } })
  await state.fetchDevices()
  state.form.package_name = 'com.example.app'
  await state.submit()
  assert.equal(state.uncertainSubmission.value, true)
  state.form.package_name = 'changed.by.background'
  await state.submit()
  const posts = apiCalls.filter(([name]) => name === 'runStartupTest')
  assert.deepEqual(posts[0][1], posts[1][1])
  assert.equal(state.uncertainSubmission.value, false)
})

test('cancel is latched, uses task id and preserves the request until acknowledged', async () => {
  const { state, apiCalls } = collectSetup('special/Startup.vue', { exports: 'stopTask' })
  const task = { id: 8, status: 'RUNNING' }
  await state.stopTask(task)
  await state.stopTask(task)
  assert.deepEqual(apiCalls.filter(([name]) => name === 'stopStartupTask'), [['stopStartupTask', 8]])
  assert.equal(task.cancel_requested, true)
})

test('both pages probe client mode on mount alongside their list refresh', async () => {
  const fluency = collectSetup('special/Fluency.vue', { exports: 'refreshAll,isClientApp' })
  await fluency.mounted[0]()
  assert.ok(fluency.apiCalls.some(([name]) => name === 'detectClientApp'), 'fluency probes on mount')

  const startup = collectSetup('special/Startup.vue', { exports: 'refreshAll,isClientApp' })
  await startup.mounted[0]()
  assert.ok(startup.apiCalls.some(([name]) => name === 'detectClientApp'), 'startup probes on mount')
})

test('templates keep fluency gating and capability-based startup controls', () => {
  const fluency = vueSource('special/Fluency.vue')
  assert.match(fluency, /const CLIENT_COLLECT_HINT = '本地化采集后续版本提供'/)
  assert.match(fluency, /:disabled="Boolean\(activeSession\) \|\| isClientApp"/, 'start button disabled in client mode')
  assert.match(fluency, /:disabled="!activeSession \|\| isClientApp"/, 'stop button disabled in client mode')
  assert.match(fluency, /v-if="isClientApp"/, 'degradation banner only in client mode')
  assert.match(fluency, /:title="CLIENT_COLLECT_HINT"/)
  assert.match(fluency, /detectClientApp\(\)\n    await refreshAll\(\)/, 'probe runs on mount alongside the session list')

  const startup = vueSource('special/Startup.vue')
  assert.match(startup, /!capabilitiesReady \|\| devicesLoading/)
  assert.doesNotMatch(startup, /:disabled="isClientApp"/)
  assert.match(startup, /capabilityError/)
  assert.match(startup, /getStartupDevices\(/)
  assert.match(startup, /api\.stopStartupTask\(/)
  assert.doesNotMatch(startup.match(/<script setup>([\s\S]*?)<\/script>/)[1], /new WebSocket|from 'axios'|[^.\w]fetch\(/)
  assert.doesNotMatch(fluency.match(/<script setup>([\s\S]*?)<\/script>/)[1], /new WebSocket|from 'axios'|[^.\w]fetch\(/)
})
