import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// P3 调用链核对（定时任务 / Fastbot / 流畅度 / 冷热启动 / 运行大盘）：
// - 前端调用面全部走 src/api/index.js 单一出口（无裸 axios/WebSocket/fetch），
//   桌面引擎按同路径拦截（本地调度/本地采集）后前端零改动；
// - 载荷形状（strategy_config._task_type、fb_*、run/startup 字段）是引擎本地
//   触发与本地采集必须复刻的契约，此处锁定防止静默漂移；
// - 定时任务 CRUD 形状与平台 ScheduledTaskCreate/Read 同源（strategy_config
//   为 Dict[str, Any]，类型附加键 _task_type/fb_* 均在其中）。
// 与仓库既有测试一致，用 node:test + 依赖注入执行 api 层（非 vitest，见 README/CI）。

const stripImports = source => source.replace(/^import [\s\S]*? from ['"][^'"]+['"]\s*\n/gm, '')
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')

function apiSetup() {
  const calls = []
  const instance = {
    interceptors: { request: { use: () => {} }, response: { use: () => {} } },
    get: (...args) => { calls.push(['get', ...args]); return Promise.resolve({ data: {} }) },
    post: (...args) => { calls.push(['post', ...args]); return Promise.resolve({ data: {} }) },
    put: (...args) => { calls.push(['put', ...args]); return Promise.resolve({ data: {} }) },
    patch: (...args) => { calls.push(['patch', ...args]); return Promise.resolve({ data: {} }) },
    delete: (...args) => { calls.push(['delete', ...args]); return Promise.resolve({ data: {} }) },
  }
  const source = stripImports(read('../src/api/index.js'))
    .replace('export default', 'const __default =')
    .replace(/export const/g, 'const')
  const deps = { axios: { create: () => instance }, normalizeError: () => {}, window: {} }
  const exposed = new Function(...Object.keys(deps), source + '\nreturn { api: __default }')(...Object.values(deps))
  return { ...exposed, calls }
}

test('scheduled-task api face is the cloud CRUD+toggle shape (TaskList zero-change)', async () => {
  const { api, calls } = apiSetup()
  await api.getTasks({ skip: 0, limit: 20 })
  await api.createTask({ name: 'n' })
  await api.updateTask(3, { name: 'n' })
  await api.toggleTask(3)
  await api.deleteTask(3)
  assert.deepEqual(calls.map(c => [c[0], c[1]]), [
    ['get', '/tasks/'],
    ['post', '/tasks/'],
    ['put', '/tasks/3'],
    ['patch', '/tasks/3/toggle'],
    ['delete', '/tasks/3'],
  ])
})

test('fastbot / fluency / startup api face matches the platform router paths', async () => {
  const { api, calls } = apiSetup()
  await api.runFastbot({ device_serial: 'USB1' })
  await api.getFastbotTasks({ limit: 1 })
  await api.getFastbotTask(9)
  await api.getFastbotReport(9)
  await api.getFastbotReplay(9, 'a.mp4')
  await api.deleteFastbotTask(9)
  await api.getFastbotDevices()
  await api.startFluencySession({ package_name: 'a.b' })
  await api.addFluencyMarker(9, { label: 'L' })
  await api.stopFluencySession(9)
  await api.getFluencySessions({ limit: 20 })
  await api.runStartupTest({ package_name: 'a.b' })
  await api.getStartupTasks({ limit: 20 })
  assert.deepEqual(calls.map(c => [c[0], c[1]]), [
    ['post', '/fastbot/run'],
    ['get', '/fastbot/tasks'],
    ['get', '/fastbot/tasks/9'],
    ['get', '/fastbot/reports/9'],
    ['get', '/fastbot/replays/9/a.mp4'],
    ['delete', '/fastbot/tasks/9'],
    ['get', '/fastbot/devices'],
    ['post', '/fastbot/fluency/start'],
    ['post', '/fastbot/fluency/9/markers'],
    ['post', '/fastbot/fluency/9/stop'],
    ['get', '/fastbot/fluency/sessions'],
    ['post', '/fastbot/startup/run'],
    ['get', '/fastbot/startup/tasks'],
  ])
})

test('dashboard is a pure pass-through read face', async () => {
  const { api, calls } = apiSetup()
  await api.getDashboardOverview({ from: 'x' })
  await api.getDashboardStats()
  assert.deepEqual(calls.map(c => [c[0], c[1]]), [
    ['get', '/reports/dashboard/overview'],
    ['get', '/reports/dashboard/stats'],
  ])
})

test('P3 views talk only through the api layer (engine interception needs zero UI change)', () => {
  const views = {
    TaskList: read('../src/views/tasks/TaskList.vue'),
    FastbotDashboard: read('../src/views/fastbot/FastbotDashboard.vue'),
    Fluency: read('../src/views/special/Fluency.vue'),
    Startup: read('../src/views/special/Startup.vue'),
  }
  for (const [name, source] of Object.entries(views)) {
    const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1]
    assert.doesNotMatch(script, /new WebSocket|from 'axios'|from "axios"|[^.\w]fetch\(/, `${name} must not open its own transport`)
    assert.match(script, /import api from '@\/api'/, `${name} must use the shared api outlet`)
  }
  // 引擎拦截面（engine/local_api.py）与云端 /api/tasks、/api/fastbot 同路径，
  // 前端不感知本地/云端分叉：以下方法即调用面全集。
  assert.match(views.TaskList, /api\.getTasks\(/)
  assert.match(views.TaskList, /api\.createTask\(/)
  assert.match(views.TaskList, /api\.updateTask\(/)
  assert.match(views.TaskList, /api\.toggleTask\(/)
  assert.match(views.TaskList, /api\.deleteTask\(/)
  assert.match(views.FastbotDashboard, /api\.runFastbot\(/)
  assert.match(views.Fluency, /api\.startFluencySession\(/)
  assert.match(views.Startup, /api\.runStartupTest\(/)
})

test('scheduled task payload keeps type markers inside strategy_config (client-tasks pull parity)', () => {
  const source = read('../src/views/tasks/TaskList.vue')
  // 本地调度拉取（GET /api/client-tasks）返回 ScheduledTask 全字段，
  // strategy_config 必须原样携带以下键，客户端才能复刻同一次触发。
  assert.match(source, /payload\.strategy_config\._task_type = form\.task_type/)
  assert.match(source, /payload\.strategy_config\.fb_package_name = form\.fb_package_name/)
  assert.match(source, /payload\.strategy_config\.fb_duration = form\.fb_duration_min \* 60/)
  assert.match(source, /payload\.strategy_config\.fb_throttle = form\.fb_throttle/)
  assert.match(source, /payload\.strategy_config\.fb_ignore_crashes = form\.fb_ignore_crashes/)
  assert.match(source, /strategy: form\.strategy/)
  assert.match(source, /device_serials: form\.task_type === 'api' \? \[\] : form\.device_serials/)
  // 四种调度策略的 strategy_config 键与平台 scheduler_service 解析面一致
  assert.match(source, /payload\.strategy_config = \{ hour: h, minute: m \}/)
  assert.match(source, /payload\.strategy_config = \{ days: form\.weekly_days, hour: h, minute: m \}/)
  assert.match(source, /payload\.strategy_config = \{ interval_value: form\.interval_value, interval_unit: form\.interval_unit \}/)
  assert.match(source, /payload\.strategy_config = \{ run_date: form\.once_datetime \}/)
})

test('fastbot and startup run payloads stay on the localized-collect contract', () => {
  const fastbot = read('../src/views/fastbot/FastbotDashboard.vue')
  for (const key of [
    'package_name', 'duration', 'throttle', 'enable_performance_monitor', 'enable_jank_frame_monitor',
    'enable_local_replay', 'ignore_crashes', 'capture_log', 'pct_touch', 'pct_motion', 'pct_syskeys', 'pct_majornav',
  ]) {
    assert.ok(fastbot.includes(`${key}:`), `fastbot run payload must keep ${key}`)
  }
  assert.match(fastbot, /device_serial: serial/, 'runFastbot is per-device (multi-device = N posts)')

  const startup = read('../src/views/special/Startup.vue')
  for (const key of ['package_name', 'activity_name', 'device_serials', 'startup_modes', 'iterations', 'cooldown_sec', 'capture_log', 'ready_check', 'perfetto_slow_trace']) {
    assert.ok(startup.includes(`${key}:`), `startup payload must keep ${key}`)
  }

  const fluency = read('../src/views/special/Fluency.vue')
  for (const key of ['package_name', 'device_serial', 'enable_performance_monitor', 'enable_jank_frame_monitor', 'capture_log', 'auto_launch_app']) {
    assert.ok(fluency.includes(`${key}:`), `fluency start payload must keep ${key}`)
  }
})
