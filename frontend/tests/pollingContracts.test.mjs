import assert from 'node:assert/strict'
import { readFile, readdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

import { pollRequestConfig } from '../src/utils/polling.js'

// A 组（前端轮询 + 投屏）修复契约：
// A1 全部轮询点走 utils/polling.js 的统一门闩（在途不重叠 + 可见性暂停 + 完成后续排）
// A2 轮询类 GET 显式短超时（api 层默认 8s，可覆盖；全局 30s 保留）
// A3 ScrcpyPlayer 非主动断流有限退避自动重连（封顶后「点击重连」）+ 遮罩不吞点击
// A4 useDeviceDump / InspectionLivePanel 的条件门控 loading 复位改为无条件

const read = path => readFile(new URL(`../src/${path}`, import.meta.url), 'utf8')

const POLLING_SITES = [
  'composables/useDeviceStreams.js',
  'composables/useLivePreviewPolling.js',
  'components/InspectionLivePanel.vue',
  'views/cases/CaseList.vue',
  'views/cases/CaseEditor.vue',
  'views/scenarios/ScenarioList.vue',
  'views/scenarios/ScenarioEditor.vue',
  'views/reports/ReportList.vue',
  'views/reports/CompatibilityReportDetail.vue',
  'views/reports/InspectionReportDetail.vue',
  'views/fastbot/FastbotDashboard.vue',
  'views/special/Fluency.vue',
  'views/special/Startup.vue',
  'views/special/Inspection.vue',
  'views/devices/DeviceCenter.vue',
  'views/dashboard/DashboardView.vue',
]

test('every polling site runs through the shared polling loop (latch + visibility + reschedule)', async () => {
  for (const path of POLLING_SITES) {
    const source = await read(path)
    assert.match(
      source,
      /createPollingLoop\(/,
      `${path} must build its poller with createPollingLoop (in-flight latch + hidden-page pause)`,
    )
    assert.doesNotMatch(
      source,
      /setInterval\(/,
      `${path} must not keep a bare setInterval poll (it can pile up pending XHR)`,
    )
  }
})

test('only local non-network interval timers remain outside the polling util', async () => {
  const srcRoot = fileURLToPath(new URL('../src', import.meta.url))
  const offenders = []
  const walk = async dir => {
    for (const entry of await readdir(dir, { withFileTypes: true })) {
      const full = `${dir}/${entry.name}`
      if (entry.isDirectory()) {
        await walk(full)
        continue
      }
      if (!/\.(js|vue)$/.test(entry.name)) continue
      const source = await readFile(full, 'utf8')
      if (!/setInterval\(/.test(source)) continue
      if (full.endsWith('utils/polling.js')) continue // 注释中提及
      // 允许清单：本地 FPS/追赶实时边界/解码统计计时器，不发网络请求
      const allowed = [
        'composables/useWebCodecsDecoder.js',
        'components/ScrcpyPlayer.vue',
        'components/IosMjpegPlayer.vue',
      ]
      const relative = full.slice(srcRoot.length + 1)
      if (!allowed.includes(relative)) offenders.push(relative)
    }
  }
  await walk(srcRoot)
  assert.deepEqual(offenders, [], `network polling must not use setInterval: ${offenders.join(', ')}`)
})

test('polling-class GETs carry an explicit short timeout while one-shot detail GETs use the 30s global', async () => {
  const apiSource = await read('api/index.js')
  assert.match(apiSource, /timeout: 30000/, 'global axios default stays 30s')
  assert.match(apiSource, /const POLL_GET_TIMEOUT_MS = 8000/)
  assert.match(apiSource, /const DETAIL_GET_TIMEOUT_MS = 30000/, '一次性大载荷 GET 显式全局 30s')
  for (const method of [
    'getActiveRuns(kind, targetId, config = {})',
    'getDevices(config = {})',
    'getDeviceList(options = {})',
    'getInspectionLive(id, config = {})',
    'getInspectionRuns(params, config = {})',
    'getFastbotTasks(params, config = {})',
    'getStartupTasks(params, config = {})',
    'getFluencySessions(params, config = {})',
    'getCompatibilityRuns(params, config = {})',
  ]) {
    assert.ok(apiSource.includes(method), `polling GET must accept a per-call config override: ${method}`)
    assert.ok(
      apiSource.includes('POLL_GET_TIMEOUT_MS'),
      'polling GETs default to POLL_GET_TIMEOUT_MS',
    )
  }
  // F7：报告详情等一次性大载荷 getter 显式 30s，8s 只留给真轮询
  assert.match(
    apiSource,
    /getInspectionRun\(id, config = \{\}\) \{[\s\S]*?api\.get\(`\/inspections\/runs\/\$\{id\}`, \{ timeout: DETAIL_GET_TIMEOUT_MS, \.\.\.config \}\)/,
    'getInspectionRun 报告详情走全局 30s',
  )
  assert.match(
    apiSource,
    /getCompatibilityRun\(id, config = \{\}\) \{[\s\S]*?api\.get\(`\/compatibility\/runs\/\$\{id\}`, \{ timeout: DETAIL_GET_TIMEOUT_MS, \.\.\.config \}\)/,
    'getCompatibilityRun 报告详情走全局 30s',
  )
  // 方法体级断言（跨方法的惰性匹配会误伤后文的轮询方法）
  const methodBody = (src, sig) => {
    const start = src.indexOf(sig)
    assert.ok(start >= 0, `找不到方法签名：${sig}`)
    return src.slice(start, src.indexOf('\n    },', start))
  }
  assert.doesNotMatch(
    methodBody(apiSource, 'getInspectionRun(id, config = {})'),
    /POLL_GET_TIMEOUT_MS/,
    'getInspectionRun 不得再套轮询短超时',
  )
  assert.doesNotMatch(
    methodBody(apiSource, 'getCompatibilityRun(id, config = {})'),
    /POLL_GET_TIMEOUT_MS/,
    'getCompatibilityRun 不得再套轮询短超时',
  )
  assert.match(
    apiSource,
    /get: \(path, params, config\) => api\.get\(`\/api-testing\$\{path\}`, \{ timeout: DETAIL_GET_TIMEOUT_MS, params, \.\.\.config \}\)/,
    'api-testing 一次性 GET 默认全局 30s',
  )
  // 短超时确实被用上（而不是只定义常量）
  assert.ok((apiSource.match(/timeout: (?:options\.timeout \?\? )?POLL_GET_TIMEOUT_MS/g) || []).length >= 10)
})

test('api-testing true polls opt into the 8s short timeout at the call site (F7)', async () => {
  // 8s 只留给真轮询：debug 会话轮询 / RunList 定时复查 / RunDetail 运行中复查
  const debugSource = await read('composables/useApiDebug.js')
  assert.match(
    debugSource,
    /api\.apiTesting\.get\(`\/debug-sessions\/\$\{sessionId\}`, undefined, pollRequestConfig\(\)\)/,
    'debug 会话轮询显式短超时',
  )
  const runListSource = await read('views/api-testing/RunList.vue')
  assert.match(
    runListSource,
    /api\.apiTesting\.get\('\/runs',[\s\S]*?quiet \? pollRequestConfig\(\) : undefined\)/,
    'RunList 定时复查显式短超时，首载走默认 30s',
  )
  const runDetailSource = await read('views/api-testing/RunDetail.vue')
  assert.match(
    runDetailSource,
    /api\.apiTesting\.get\(`\/runs\/\$\{route\.params\.id\}`, undefined, run\.value \? pollRequestConfig\(\) : undefined\)/,
    'RunDetail 首载大载荷 30s、运行中复查显式短超时',
  )
})

test('CompatibilityReportDetail 真轮询传 pollRequestConfig()，一次性首载保持 30s（G6）', async () => {
  const source = await read('views/reports/CompatibilityReportDetail.vue')

  // 轮询 tick（8s 真轮询）必须显式短超时
  assert.match(
    source,
    /tick: async \(\) => \{[\s\S]*?await fetchRun\(pollRequestConfig\(\)\)/,
    'runPollLoop tick 调 fetchRun(pollRequestConfig())（旧实现不传 config，每轮挂满 30s 默认超时）',
  )
  // 一次性路径（首载 / 取消后刷新 / 手动重载）保持默认 30s
  assert.match(
    source,
    /await Promise\.all\(\[fetchDevices\(\), fetchRun\(\)\]\)/,
    'refreshAll 一次性首载不传 poll config（保持 30s 大载荷超时）',
  )
  assert.match(
    source,
    /const fetchRun = async \(config\) => \{[\s\S]*?api\.getCompatibilityRun\(id, config\)/,
    'fetchRun 把 per-call config 透传给 api.getCompatibilityRun',
  )
  assert.match(
    source,
    /const cancelRun = async[\s\S]*?await fetchRun\(\)/,
    'cancelRun 后的一次性刷新同样不传 poll config（保持 30s）',
  )
})

test('CompatibilityReportDetail fetchRun 行为级：config 透传决定请求超时', async () => {
  const source = await read('views/reports/CompatibilityReportDetail.vue')
  const start = source.indexOf('const fetchRun = async')
  const end = source.indexOf('const refreshAll')
  assert.ok(start >= 0 && end > start, '提取 fetchRun 失败')
  const fnSource = source.slice(start, end)

  const seen = []
  const api = {
    getCompatibilityRun: async (id, config) => {
      seen.push([id, config])
      return { data: { id, status: 'running' } }
    },
  }
  const deps = {
    api,
    route: { params: { id: '5' } },
    loading: { value: false },
    loadError: { value: '' },
    run: { value: null },
    ElMessage: { error: () => {} },
    Number,
  }
  const fetchRun = new Function(
    ...Object.keys(deps),
    `${fnSource}\nreturn fetchRun`,
  )(...Object.values(deps))

  // 真轮询：显式 8s 短超时
  await fetchRun(pollRequestConfig())
  assert.deepEqual(seen[0], [5, { timeout: 8000 }], '轮询路径带 pollRequestConfig()（8s）')
  // 一次性首载：不传 config，由 api 层默认 30s（DETAIL_GET_TIMEOUT_MS）
  await fetchRun()
  assert.deepEqual(seen[1], [5, undefined], '首载不传 config（保持 api 层 30s）')
})

test('DashboardView 自动刷新勾选按状态 start/stop（取消勾选不得留循环空转，F6）', async () => {
  const source = await read('views/dashboard/DashboardView.vue')
  assert.match(
    source,
    /watch\(\s*\(\) => autoRefresh\.value,[\s\S]*?\(enabled\) => \{[\s\S]*?if \(enabled\) startPolling\(\)\s*else stopPolling\(\)/,
    '勾选状态 watch 按状态 start/stop',
  )
  assert.match(
    source,
    /const startPolling = \(\) => \{[\s\S]*?if \(!autoRefresh\.value\) return[\s\S]*?dashboardPollLoop\.start\(\)/,
    '取消勾选后可见性恢复/筛选变更不得复活循环',
  )
})

test('chained self-rescheduling polls skip issuing requests while the page is hidden', async () => {
  for (const path of ['composables/useApiDebug.js', 'views/api-testing/RunDetail.vue', 'views/api-testing/RunList.vue']) {
    const source = await read(path)
    assert.match(
      source,
      /typeof document !== 'undefined' && document\.hidden/,
      `${path} must pause its chain while the page is hidden`,
    )
  }
})

test('device stream polling gates overlapping fetches and uses short-timeout requests', async () => {
  const source = await read('composables/useDeviceStreams.js')
  assert.match(source, /createGatedTask\(/, 'fetchDevices keeps the in-flight latch (DashboardView pattern)')
  assert.match(source, /pollRequestConfig\(\)/, 'device/stream list GETs use the poll short timeout')
})

test('ScrcpyPlayer auto-reconnects unexpected closes with bounded backoff and a manual escape hatch', async () => {
  const source = await read('components/ScrcpyPlayer.vue')
  assert.match(source, /const AUTO_RECONNECT_BASE_DELAY_MS = 1000/)
  assert.match(source, /const AUTO_RECONNECT_MAX_DELAY_MS = 8000/)
  assert.match(source, /const AUTO_RECONNECT_MAX_ATTEMPTS = 5/)
  assert.match(source, /closedIntentionally/, 'intentional disconnects never auto-reconnect')
  assert.match(
    source,
    /autoReconnectAttempts < AUTO_RECONNECT_MAX_ATTEMPTS[\s\S]*?Math\.min\(\s*AUTO_RECONNECT_BASE_DELAY_MS \* \(2 \*\* \(autoReconnectAttempts - 1\)\),\s*AUTO_RECONNECT_MAX_DELAY_MS,/,
    'non-initiated close schedules 1s/2s/4s/8s-capped retries',
  )
  assert.match(source, /autoReconnectGaveUp\.value = true/, 'after the cap the player waits for the user')
  assert.match(source, /'点击重连'/, 'gave-up state shows the 点击重连 button')
  assert.match(source, /function handleManualReconnect\(\)/, 'manual reconnect resets the retry budget')
  assert.match(source, /ws\.onopen[\s\S]*?autoReconnectAttempts = 0/, 'a successful open resets the budget')
  // 一次性票据 URL（巡检视频）不套用自动重连，交由调用方重建会话
  assert.match(source, /if \(closedIntentionally \|\| props\.streamUrl\) return/)
})

test('ScrcpyPlayer overlay no longer swallows clicks; its buttons stay clickable', async () => {
  const source = await read('components/ScrcpyPlayer.vue')
  assert.match(
    source,
    /\.player-overlay \{[^}]*pointer-events: none;/s,
    'the disconnected overlay must not intercept clicks',
  )
  assert.match(
    source,
    /\.player-overlay :deep\(\.el-button\) \{\s*pointer-events: auto;/,
    'overlay buttons keep their click target',
  )
})

test('conditional loading resets are unconditional (stuck-loading landmines removed)', async () => {
  const dumpSource = await read('composables/useDeviceDump.js')
  assert.doesNotMatch(
    dumpSource,
    /finally \{[\s\S]*?if \(isStageActive\.value\) \{[\s\S]*?loading\.value = false/,
    'fetchDump must reset loading even when the stage went inactive mid-request',
  )
  assert.doesNotMatch(
    dumpSource,
    /if \(isStageActive\.value\) \{\s*liveHierarchyLoading\.value = false/,
    'fetchLiveHierarchy must reset its loading flag unconditionally',
  )
  const panelSource = await read('components/InspectionLivePanel.vue')
  assert.doesNotMatch(
    panelSource,
    /if \(generation === requestGeneration\) loading\.value = false/,
    'session switching must not leave the live panel loading forever',
  )
  assert.match(
    panelSource,
    /finally \{\s*\/\/ 无条件复位 loading[\s\S]*?loading\.value = false/,
  )
})
