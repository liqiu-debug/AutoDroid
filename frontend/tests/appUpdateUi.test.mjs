import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, ref } from 'vue'

import { checkForUpdate, runUpdateInstall } from '../src/utils/appUpdate.js'

// 桌面端「检查更新」前端流程：Navbar 账号下拉入口（clientMode 门控）+ 确认框
// 「发现新版本 x.y.z（当前 a.b.c）」→「下载安装」+ 下载进度/完成/失败提示 +
// 启动静默检查（每会话一次，ElNotification 点击开同一确认框）+ 桥缺失降级。
// 与仓库既有测试一致，用 node:test + 依赖注入执行 <script setup>/composable（非 vitest）。

const stripImports = source => source.replace(/^import [\s\S]*? from ['"][^'"]+['"]\s*\n/gm, '')
const read = path => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')

const messages = record => Object.fromEntries(
  ['success', 'warning', 'error', 'info'].map(kind => [kind, (...args) => record.push([kind, ...args])])
)

const PKG = {
  id: 7,
  platform: 'macos',
  package_name: 'autodroid-desktop',
  app_name: 'AutoDroid',
  version_name: '1.2.3',
  file_size: 52.4,
  file_path: 'uploads/apps/ab12cd34.dmg',
}

/** 假壳桥（window.desktop）：installUpdate 期间回放 events，再按 settle 收尾。 */
function fakeDesktop({ events = [], settle = { type: 'resolve' }, clientInfo = { platform: 'darwin', version: '1.0.0' }, withTray = false } = {}) {
  const state = { installCalls: [], unsubscribed: false, subscriber: null, trayCallback: null, trayUnsubscribed: false }
  return {
    state,
    getClientInfo: async () => clientInfo,
    onUpdateProgress: cb => {
      state.subscriber = cb
      return () => { state.unsubscribed = true }
    },
    installUpdate: async payload => {
      state.installCalls.push(payload)
      for (const evt of events) state.subscriber?.(evt)
      if (settle.type === 'reject') throw new Error(settle.message || '下载失败')
      return settle.value || { launched: true }
    },
    ...(withTray ? {
      onCheckUpdateRequest: cb => {
        state.trayCallback = cb
        return () => { state.trayUnsubscribed = true }
      },
    } : {}),
  }
}

/** 依赖注入执行 useAppUpdate composable：真实 checkForUpdate/runUpdateInstall，假 api/壳桥/通知。 */
function updateSetup({ clientMode = true, desktop = null, listItems = [], confirm = 'resolve', queryError = null } = {}) {
  const apiCalls = []
  const notices = []
  const confirms = []
  const notifications = []
  const detectCalls = []
  const isClientApp = ref(!!clientMode)

  const api = {
    getPackages: async params => {
      apiCalls.push(['getPackages', params])
      if (queryError) throw queryError
      return { data: { items: listItems, total: listItems.length } }
    },
  }
  const useClientApp = () => ({
    isClientApp,
    detectClientApp: async () => { detectCalls.push(true); return isClientApp.value },
  })
  const ElMessage = messages(notices)
  const ElMessageBox = {
    confirm: async (message, title, options) => {
      confirms.push([message, title, options])
      if (confirm === 'cancel') throw new Error('cancel')
      return 'confirm'
    },
  }
  const ElNotification = options => {
    notifications.push(options)
    return { close: () => { options.closed = true } }
  }

  const deps = {
    ref,
    ElMessage,
    ElMessageBox,
    ElNotification,
    api,
    useClientApp,
    checkForUpdate,
    runUpdateInstall,
    window: desktop ? { desktop } : {},
    localStorage: { getItem: key => (key === 'token' ? 'tok-1' : null) },
    location: { origin: 'http://127.0.0.1:5800' },
  }
  const source = stripImports(read('composables/useAppUpdate.js')).replace('export function useAppUpdate', 'function useAppUpdate')
  const useAppUpdate = new Function(...Object.keys(deps), source + '\nreturn useAppUpdate')(...Object.values(deps))
  return { useAppUpdate, apiCalls, notices, confirms, notifications, detectCalls, isClientApp }
}

test('web mode has no entry and never queries the update source', async () => {
  const { useAppUpdate, apiCalls, notices, notifications } = updateSetup({ clientMode: false, desktop: null })
  const app = useAppUpdate()
  await app.initSilentUpdateCheck()
  await app.startUpdateCheck()
  assert.deepEqual(apiCalls, [], 'no packages query in pure web mode')
  assert.deepEqual(notices, [])
  assert.deepEqual(notifications, [])
})

test('silent startup check runs at most once per session and stays quiet without an update', async () => {
  const desktop = fakeDesktop({ clientInfo: { platform: 'darwin', version: '1.2.3' } })
  const { useAppUpdate, apiCalls, notifications } = updateSetup({ clientMode: true, desktop, listItems: [PKG] })
  const app = useAppUpdate()
  await app.initSilentUpdateCheck()
  await app.initSilentUpdateCheck()
  assert.deepEqual(apiCalls, [['getPackages', { package_name: 'autodroid-desktop', latest_only: true }]], 'contract query shape, once per session')
  assert.deepEqual(notifications, [], 'no update means no notification')
})

test('silent check notifies on a newer version and the click opens the same confirm box', async () => {
  const desktop = fakeDesktop()
  const { useAppUpdate, apiCalls, notices, confirms, notifications } = updateSetup({ clientMode: true, desktop, listItems: [PKG] })
  const app = useAppUpdate()
  await app.initSilentUpdateCheck()
  assert.equal(apiCalls.length, 1)

  assert.equal(notifications.length, 1)
  const discovery = notifications[0]
  assert.equal(discovery.id, 'autodroid-update-discovery')
  assert.match(discovery.message, /发现新版本 1\.2\.3（当前 1\.0\.0）/)
  assert.equal(typeof discovery.onClick, 'function')

  await discovery.onClick()
  assert.equal(confirms.length, 1, 'notification click opens the shared confirm box')
  assert.equal(confirms[0][0], '发现新版本 1.2.3（当前 1.0.0）')
  assert.equal(confirms[0][1], '检查更新')
  assert.equal(confirms[0][2].confirmButtonText, '下载安装')
  assert.equal(desktop.state.installCalls.length, 1)
  assert.ok(notices.some(([kind, text]) => kind === 'success' && text === '安装完成后请重启应用'))
})

test('manual check with no update says so and never opens the confirm box', async () => {
  const desktop = fakeDesktop({ clientInfo: { platform: 'darwin', version: '1.2.3' } })
  const { useAppUpdate, notices, confirms } = updateSetup({ clientMode: true, desktop, listItems: [PKG] })
  await useAppUpdate().startUpdateCheck()
  assert.deepEqual(notices, [['info', '已是最新版本（1.2.3）']])
  assert.deepEqual(confirms, [])
})

test('manual check reports a missing desktop package instead of claiming up-to-date', async () => {
  const desktop = fakeDesktop()
  const { useAppUpdate, notices } = updateSetup({ clientMode: true, desktop, listItems: [] })
  await useAppUpdate().startUpdateCheck()
  assert.deepEqual(notices, [['info', '暂未发现可用的更新包']])
})

test('download install streams percents and finishes with the restart notice', async () => {
  const desktop = fakeDesktop({
    events: [
      { phase: 'downloading', percent: 37 },
      { phase: 'downloading', percent: 80 },
      { phase: 'done', path: '/Users/x/Library/Application Support/updates/AutoDroid-1.2.3.dmg' },
    ],
    clientInfo: { platform: 'darwin', version: '1.0.0' },
  })
  const { useAppUpdate, confirms, notices, notifications } = updateSetup({ clientMode: true, desktop, listItems: [PKG] })
  await useAppUpdate().startUpdateCheck()

  assert.deepEqual(confirms[0], [
    '发现新版本 1.2.3（当前 1.0.0）',
    '检查更新',
    { type: 'info', confirmButtonText: '下载安装', cancelButtonText: '暂不更新' },
  ])
  assert.deepEqual(desktop.state.installCalls[0], {
    url: 'http://127.0.0.1:5800/api/packages/7/download?token=tok-1',
    fileName: 'AutoDroid-1.2.3.dmg',
    version: '1.2.3',
  })

  const progress = notifications.filter(item => item.id === 'autodroid-update-progress')
  assert.deepEqual(progress.map(item => item.message), ['正在下载 1.2.3：37%', '正在下载 1.2.3：80%'])
  assert.equal(progress[0].duration, 0, 'progress notice stays until the terminal phase')
  assert.equal(progress[1].closed, true, 'progress notice is closed on completion')
  assert.deepEqual(notices.filter(([kind]) => kind === 'success').map(([, text]) => text), ['安装完成后请重启应用'])
  assert.equal(desktop.state.unsubscribed, true)
})

test('declining the confirm box leaves the shell untouched', async () => {
  const desktop = fakeDesktop()
  const { useAppUpdate, confirms } = updateSetup({ clientMode: true, desktop, listItems: [PKG], confirm: 'cancel' })
  await useAppUpdate().startUpdateCheck()
  assert.equal(confirms.length, 1)
  assert.deepEqual(desktop.state.installCalls, [], 'no download before the user opts in')
})

test('a failed download shows one retryable error despite event and rejection both firing', async () => {
  const desktop = fakeDesktop({
    events: [{ phase: 'downloading', percent: 12 }, { phase: 'error', message: '网络中断' }],
    settle: { type: 'reject', message: '网络中断' },
    clientInfo: { platform: 'darwin', version: '1.0.0' },
  })
  const { useAppUpdate, notices, notifications } = updateSetup({ clientMode: true, desktop, listItems: [PKG] })
  await useAppUpdate().startUpdateCheck()

  const errors = notices.filter(([kind]) => kind === 'error')
  assert.deepEqual(errors.map(([, text]) => text), ['更新失败：网络中断（可重试）'])
  const progress = notifications.filter(item => item.id === 'autodroid-update-progress')
  assert.equal(progress[progress.length - 1].closed, true, 'progress notice closes on failure')
})

test('a rejection without an error event still surfaces the retryable error', async () => {
  const desktop = fakeDesktop({
    events: [{ phase: 'downloading', percent: 5 }],
    settle: { type: 'reject', message: '磁盘空间不足' },
    clientInfo: { platform: 'darwin', version: '1.0.0' },
  })
  const { useAppUpdate, notices } = updateSetup({ clientMode: true, desktop, listItems: [PKG] })
  await useAppUpdate().startUpdateCheck()
  assert.deepEqual(notices.filter(([kind]) => kind === 'error').map(([, text]) => text), ['更新失败：磁盘空间不足（可重试）'])
})

test('a missing bridge degrades to a notice and the silent check stays silent', async () => {
  const manual = updateSetup({ clientMode: true, desktop: null, listItems: [PKG] })
  await manual.useAppUpdate().startUpdateCheck()
  assert.deepEqual(manual.notices, [['info', '当前环境不支持检查更新']])
  assert.deepEqual(manual.apiCalls, [], 'degrades before querying packages')

  const silent = updateSetup({ clientMode: true, desktop: null, listItems: [PKG] })
  await silent.useAppUpdate().initSilentUpdateCheck()
  assert.deepEqual(silent.notices, [])
  assert.deepEqual(silent.notifications, [])
})

test('an unsupported platform degrades the same way', async () => {
  const desktop = fakeDesktop({ clientInfo: { platform: 'linux', version: '1.0.0' } })
  const { useAppUpdate, notices } = updateSetup({ clientMode: true, desktop, listItems: [PKG] })
  await useAppUpdate().startUpdateCheck()
  assert.deepEqual(notices, [['info', '当前环境不支持检查更新']])
})

test('a failed update-source query reports a Chinese error manually and stays silent on startup', async () => {
  const manual = updateSetup({ clientMode: true, desktop: fakeDesktop(), queryError: new Error('服务内部错误') })
  await manual.useAppUpdate().startUpdateCheck()
  assert.deepEqual(manual.notices, [['error', '检查更新失败：服务内部错误']])

  const silent = updateSetup({ clientMode: true, desktop: fakeDesktop(), queryError: new Error('服务内部错误') })
  await silent.useAppUpdate().initSilentUpdateCheck()
  assert.deepEqual(silent.notices, [])
})

test('tray check-update request triggers the same manual check flow', async () => {
  const upToDate = fakeDesktop({ withTray: true, clientInfo: { platform: 'darwin', version: '1.2.3' } })
  const first = updateSetup({ clientMode: true, desktop: upToDate, listItems: [PKG] })
  const unsubscribe = first.useAppUpdate().subscribeCheckUpdateRequest()
  assert.equal(typeof upToDate.state.trayCallback, 'function')
  await upToDate.state.trayCallback()
  assert.deepEqual(first.notices, [['info', '已是最新版本（1.2.3）']], 'tray runs the same check flow')

  const withUpdate = fakeDesktop({ withTray: true })
  const second = updateSetup({ clientMode: true, desktop: withUpdate, listItems: [PKG] })
  second.useAppUpdate().subscribeCheckUpdateRequest()
  await withUpdate.state.trayCallback()
  assert.equal(second.confirms[0][0], '发现新版本 1.2.3（当前 1.0.0）', 'tray opens the same confirm box')

  unsubscribe()
  assert.equal(upToDate.state.trayUnsubscribed, true)
})

test('a bridge without onCheckUpdateRequest degrades to a no-op subscription', () => {
  const desktop = fakeDesktop()
  const { useAppUpdate } = updateSetup({ clientMode: true, desktop, listItems: [PKG] })
  const unsubscribe = useAppUpdate().subscribeCheckUpdateRequest()
  assert.equal(typeof unsubscribe, 'function')
  assert.doesNotThrow(() => unsubscribe())

  const noBridge = updateSetup({ clientMode: true, desktop: null })
  assert.equal(typeof noBridge.useAppUpdate().subscribeCheckUpdateRequest(), 'function')
})

test('navbar entry is client-mode gated and shares the group with logout', () => {
  const source = read('layout/components/Navbar.vue')
  assert.match(source, /<el-dropdown-item v-if="isClientApp" command="check-update" :icon="Refresh" divided>检查更新<\/el-dropdown-item>/)
  assert.match(source, /<el-dropdown-item command="logout" :icon="SwitchButton" :divided="!isClientApp">退出登录<\/el-dropdown-item>/)
  assert.match(source, /import \{ useClientApp \} from '@\/composables\/useClientApp'/)
  assert.match(source, /if \(command === 'check-update'\) return startUpdateCheck\(\)/)
  assert.match(source, /onMounted\(\(\) => \{[\s\S]*?initSilentUpdateCheck\(\)[\s\S]*?subscribeCheckUpdateRequest\(\)[\s\S]*?\}\)/)
})

test('navbar dispatches the update entry and the silent startup check without touching logout', async () => {
  const calls = []
  const mounted = []
  const script = stripImports(read('layout/components/Navbar.vue').match(/<script setup>([\s\S]*?)<\/script>/)[1])
  const deps = {
    computed,
    onMounted: fn => mounted.push(fn),
    useUserStore: () => ({ userInfo: { username: 'tester' }, logout: () => calls.push(['logout']) }),
    useRouter: () => ({ push: path => calls.push(['push', path]) }),
    ElMessageBox: { confirm: async () => true },
    useClientApp: () => ({ isClientApp: ref(true) }),
    useAppUpdate: () => ({
      startUpdateCheck: async () => { calls.push(['startUpdateCheck']) },
      initSilentUpdateCheck: async () => { calls.push(['initSilentUpdateCheck']) },
      subscribeCheckUpdateRequest: () => { calls.push(['subscribeCheckUpdateRequest']) },
    }),
  }
  const state = new Function(...Object.keys(deps), script + '\nreturn { handleCommand }')(...Object.values(deps))

  await mounted[0]()
  await state.handleCommand('check-update')
  assert.deepEqual(calls, [
    ['initSilentUpdateCheck'],
    ['subscribeCheckUpdateRequest'],
    ['startUpdateCheck'],
  ])

  await state.handleCommand('logout')
  assert.ok(calls.some(([name]) => name === 'logout'), 'logout flow unchanged')
  assert.ok(calls.some(([name, path]) => name === 'push' && path === '/login'))
})
