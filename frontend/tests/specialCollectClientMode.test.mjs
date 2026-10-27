import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, reactive, ref } from 'vue'

// T6：客户端（桌面引擎）模式下流畅度/冷热启动是 UX 死路（/api/fastbot/fluency/*、
// /startup/* 透传云端 400「设备不在线」误导）。对齐 S9 修法：useClientApp 门控
// 禁用入口 + 提示「本地化采集后续版本提供」，纯 Web 模式行为零变化。
// 与仓库既有测试一致，用 node:test + 依赖注入执行 <script setup>（非 vitest）。

const stripImports = source => source.replace(/^import [\s\S]*? from ['"][^'"]+['"]\s*\n/gm, '')
const vueSource = name => readFileSync(new URL(`../src/views/${name}`, import.meta.url), 'utf8')
const scriptOf = name => stripImports(vueSource(name).match(/<script setup>([\s\S]*?)<\/script>/)[1])

const messages = record => Object.fromEntries(
  ['success', 'warning', 'error', 'info'].map(kind => [kind, (...args) => record.push([kind, ...args])])
)

function collectSetup(name, { clientMode = false, exports } = {}) {
  const apiCalls = []
  const notices = []
  const mounted = []
  const api = {
    getFastbotDevices: async () => { apiCalls.push(['getFastbotDevices']); return { data: [] } },
    getFluencySessions: async () => { apiCalls.push(['getFluencySessions']); return { data: [] } },
    getStartupTasks: async () => { apiCalls.push(['getStartupTasks']); return { data: { items: [], total: 0 } } },
    startFluencySession: async (payload) => { apiCalls.push(['startFluencySession', payload]); return { data: { task_id: 1, status: 'RUNNING', markers: [] } } },
    addFluencyMarker: async (taskId, payload) => { apiCalls.push(['addFluencyMarker', taskId, payload]); return { data: { task_id: taskId, status: 'RUNNING', markers: [] } } },
    stopFluencySession: async (taskId) => { apiCalls.push(['stopFluencySession', taskId]); return { data: { task_id: taskId, status: 'COMPLETED', markers: [] } } },
    runStartupTest: async (payload) => { apiCalls.push(['runStartupTest', payload]); return { data: { id: 7 } } },
  }
  const isClientApp = ref(!!clientMode)
  const detectClientApp = async () => { apiCalls.push(['detectClientApp']); return isClientApp.value }
  const deps = {
    computed, reactive, ref,
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

test('client mode gates startup submission with the degradation notice and zero writes', async () => {
  const exports = 'submit,form,selectedDevices,isClientApp'
  const { state, apiCalls, notices } = collectSetup('special/Startup.vue', { clientMode: true, exports })

  state.form.package_name = 'com.example.app'
  state.selectedDevices.value = ['USB1']
  await state.submit()
  assert.deepEqual(notices, [['info', '本地化采集后续版本提供']])
  assert.ok(!apiCalls.some(([name]) => name === 'runStartupTest'), 'no startup run behind the dead entry')
})

test('web mode keeps the startup flow unchanged', async () => {
  const exports = 'submit,form,selectedDevices,isClientApp'
  const { state, apiCalls, notices } = collectSetup('special/Startup.vue', { clientMode: false, exports })

  state.form.package_name = 'com.example.app'
  state.selectedDevices.value = ['USB1']
  await state.submit()
  assert.ok(notices.some(([kind, text]) => kind === 'success' && /已提交 1 台设备的冷热启动测试/.test(text)))
  assert.ok(apiCalls.some(([name]) => name === 'runStartupTest'))
})

test('both pages probe client mode on mount alongside their list refresh', async () => {
  const fluency = collectSetup('special/Fluency.vue', { exports: 'refreshAll,isClientApp' })
  await fluency.mounted[0]()
  assert.ok(fluency.apiCalls.some(([name]) => name === 'detectClientApp'), 'fluency probes on mount')

  const startup = collectSetup('special/Startup.vue', { exports: 'refreshAll,isClientApp' })
  await startup.mounted[0]()
  assert.ok(startup.apiCalls.some(([name]) => name === 'detectClientApp'), 'startup probes on mount')
})

test('templates gate the collect entries and show the degradation hint', () => {
  const fluency = vueSource('special/Fluency.vue')
  assert.match(fluency, /const CLIENT_COLLECT_HINT = '本地化采集后续版本提供'/)
  assert.match(fluency, /:disabled="Boolean\(activeSession\) \|\| isClientApp"/, 'start button disabled in client mode')
  assert.match(fluency, /:disabled="!activeSession \|\| isClientApp"/, 'stop button disabled in client mode')
  assert.match(fluency, /v-if="isClientApp"/, 'degradation banner only in client mode')
  assert.match(fluency, /:title="CLIENT_COLLECT_HINT"/)
  assert.match(fluency, /detectClientApp\(\)\n    await refreshAll\(\)/, 'probe runs on mount alongside the session list')

  const startup = vueSource('special/Startup.vue')
  assert.match(startup, /const CLIENT_COLLECT_HINT = '本地化采集后续版本提供'/)
  assert.match(startup, /:disabled="isClientApp" :title="isClientApp \? CLIENT_COLLECT_HINT : undefined" @click="submit"/, 'run button disabled in client mode')
  assert.match(startup, /v-if="isClientApp"/, 'degradation banner only in client mode')
  assert.match(startup, /detectClientApp\(\)\n    await refreshAll\(\)/, 'probe runs on mount alongside the task list')
  // 引擎拦截面未覆盖 fluency/startup，门控后仍只走共享 api 出口（不得新增直连）
  assert.doesNotMatch(startup.match(/<script setup>([\s\S]*?)<\/script>/)[1], /new WebSocket|from 'axios'|[^.\w]fetch\(/)
  assert.doesNotMatch(fluency.match(/<script setup>([\s\S]*?)<\/script>/)[1], /new WebSocket|from 'axios'|[^.\w]fetch\(/)
})
