import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, ref } from 'vue'

import {
  SKIPPED_UPDATE_KEY,
  applyUpdate,
  buildUpdateDialogHtml,
  checkForUpdate,
  getStagedUpdate,
  isUpdateSkipped,
  readSkippedUpdateVersion,
  rememberSkippedUpdateVersion,
  runUpdateInstall,
} from '../src/utils/appUpdate.js'
import { compareVersions } from '../src/utils/semver.js'
import { createProgressNotice } from '../src/utils/progressNotice.js'

// 桌面端「检查更新」前端流程：Navbar 账号下拉入口（clientMode 门控）+ 更新弹窗
// （新版本 + 更新提示 + 「立即更新 / 不再提示」）+ 下载进度/完成/失败提示 +
// 启动自动检查（每会话一次，有新版直接弹窗，已「不再提示」的版本保持安静）+ 桥缺失降级。
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
  changelog: '修复录制过程中偶发断连',
}

/** 假壳桥（window.desktop）：installUpdate 期间回放 events，再按 settle 收尾。 */
function fakeDesktop({ events = [], settle = { type: 'resolve' }, clientInfo = { platform: 'darwin', version: '1.0.0' }, withTray = false, release = null, staged = null, installHandler = null, applyError = null } = {}) {
  const state = { installCalls: [], unsubscribed: false, subscriber: null, trayCallback: null, trayUnsubscribed: false, applyCalls: 0, releaseQueries: 0 }
  return {
    state,
    getClientInfo: async () => clientInfo,
    onUpdateProgress: cb => {
      state.subscriber = cb
      return () => { state.unsubscribed = true }
    },
    installUpdate: async payload => {
      state.installCalls.push(payload)
      if (typeof installHandler === 'function') {
        return installHandler(payload, state)
      }
      for (const evt of events) state.subscriber?.(evt)
      if (settle.type === 'reject') throw new Error(settle.message || '下载失败')
      return settle.value || { launched: true }
    },
    checkRelease: async () => {
      state.releaseQueries += 1
      if (!release) return { ok: false, message: '更新源不可达' }
      return { ok: true, release }
    },
    getUpdateStatus: async () => ({ staged }),
    applyUpdate: async () => {
      state.applyCalls += 1
      if (applyError) throw new Error(applyError)
      return { ok: true }
    },
    ...(withTray ? {
      onCheckUpdateRequest: cb => {
        state.trayCallback = cb
        return () => { state.trayUnsubscribed = true }
      },
    } : {}),
  }
}

/**
 * 依赖注入执行 useAppUpdate composable：真实 checkForUpdate/runUpdateInstall，假 api/壳桥/通知。
 * confirm 模拟更新弹窗的用户选择：'resolve' 点「立即更新」、'cancel' 点「不再提示」、
 * 'close' 直接关掉（X / Esc，既不安也不记「不再提示」）；skippedVersion 预置已忽略版本。
 */
function updateSetup({ clientMode = true, desktop = null, listItems = [], confirm = 'resolve', queryError = null, skippedVersion = '' } = {}) {
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
    // element-plus 的 ElMessageBox 取消/关闭都以字符串 reject，这里照同样形状模拟
    confirm: async (message, title, options) => {
      confirms.push([message, title, options])
      if (confirm === 'cancel') throw 'cancel'
      if (confirm === 'close') throw 'close'
      return 'confirm'
    },
  }
  // 与 element-plus 真实行为一致：每次调用都新开一个通知（同 id 不会替换旧的）。
  // message 为渲染函数时，text() 取它当前渲染出的文本。
  const ElNotification = options => {
    const entry = {
      ...options,
      text: () => (typeof options.message === 'function' ? options.message() : options.message),
    }
    notifications.push(entry)
    return { close: () => { entry.closed = true } }
  }
  const h = (_tag, children) => children
  // 记录进度通知的文本变化序列（初始文本 + 每次 update）
  const progressTexts = []
  const trackedProgressNotice = options => {
    const notice = createProgressNotice(options)
    progressTexts.push(options.message)
    const update = notice.update
    notice.update = text => { progressTexts.push(text); update(text) }
    return notice
  }
  const store = {}
  if (skippedVersion) store[SKIPPED_UPDATE_KEY] = skippedVersion
  const localStorage = {
    store,
    getItem: key => (key === 'token' ? 'tok-1' : (store[key] ?? null)),
    setItem: (key, value) => { store[key] = String(value) },
    removeItem: key => { delete store[key] },
  }

  const deps = {
    ref,
    ElMessage,
    ElMessageBox,
    ElNotification,
    h,
    createProgressNotice: trackedProgressNotice,
    api,
    useClientApp,
    checkForUpdate,
    runUpdateInstall,
    getStagedUpdate,
    applyUpdate,
    compareVersions,
    buildUpdateDialogHtml,
    isUpdateSkipped,
    readSkippedUpdateVersion,
    rememberSkippedUpdateVersion,
    window: desktop ? { desktop } : {},
    localStorage,
    location: { origin: 'http://127.0.0.1:5800' },
  }
  const source = stripImports(read('composables/useAppUpdate.js')).replace('export function useAppUpdate', 'function useAppUpdate')
  const useAppUpdate = new Function(...Object.keys(deps), source + '\nreturn useAppUpdate')(...Object.values(deps))
  return { useAppUpdate, apiCalls, notices, confirms, notifications, progressTexts, detectCalls, isClientApp, localStorage }
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

test('silent startup check pops the update dialog with the version and the uploaded notes', async () => {
  const desktop = fakeDesktop()
  const { useAppUpdate, apiCalls, notices, confirms, notifications } = updateSetup({ clientMode: true, desktop, listItems: [PKG] })
  const app = useAppUpdate()
  await app.initSilentUpdateCheck()
  assert.equal(apiCalls.length, 1)

  assert.deepEqual(notifications, [], '启动检查不再只弹一个需要点击的通知，直接给弹窗')
  assert.equal(confirms.length, 1)
  const [html, title, options] = confirms[0]
  assert.equal(title, '发现新版本')
  assert.match(html, /新版本 <strong>1\.2\.3<\/strong>/)
  assert.match(html, /（当前 1\.0\.0）/)
  assert.match(html, /更新提示/)
  assert.match(html, /修复录制过程中偶发断连/, '弹窗展示上传包时手写的更新提示')
  assert.equal(options.dangerouslyUseHTMLString, true)
  assert.equal(options.confirmButtonText, '立即更新')
  assert.equal(options.cancelButtonText, '不再提示')
  assert.equal(options.customClass, 'autodroid-update-box')
  assert.equal(desktop.state.installCalls.length, 1, '点「立即更新」直接开始下载')
  assert.ok(notices.some(([kind, text]) => kind === 'success' && text === '安装完成后请重启应用'))
})

test('a package without notes still renders the fallback line instead of an empty dialog', async () => {
  const desktop = fakeDesktop()
  const { useAppUpdate, confirms } = updateSetup({
    clientMode: true,
    desktop,
    listItems: [{ ...PKG, changelog: '' }],
  })
  await useAppUpdate().initSilentUpdateCheck()
  assert.match(confirms[0][0], /本次更新未提供说明/)
})

test('the notes are HTML-escaped: uploaded text can never inject markup', async () => {
  const desktop = fakeDesktop()
  const { useAppUpdate, confirms } = updateSetup({
    clientMode: true,
    desktop,
    listItems: [{ ...PKG, changelog: '<img src=x onerror="alert(1)"> & 1 < 2' }],
  })
  await useAppUpdate().initSilentUpdateCheck()
  assert.doesNotMatch(confirms[0][0], /<img/)
  assert.match(confirms[0][0], /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt; &amp; 1 &lt; 2/)
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
  const { useAppUpdate, confirms, notices, notifications, progressTexts } = updateSetup({ clientMode: true, desktop, listItems: [PKG] })
  await useAppUpdate().startUpdateCheck()

  assert.deepEqual(confirms[0], [
    buildUpdateDialogHtml({ remoteVersion: '1.2.3', localVersion: '1.0.0', notes: PKG.changelog }),
    '发现新版本',
    {
      dangerouslyUseHTMLString: true,
      type: 'info',
      confirmButtonText: '立即更新',
      cancelButtonText: '不再提示',
      showClose: true,
      closeOnClickModal: false,
      closeOnPressEscape: true,
      customClass: 'autodroid-update-box',
    },
  ])
  assert.deepEqual(desktop.state.installCalls[0], {
    url: 'http://127.0.0.1:5800/api/packages/7/download?token=tok-1',
    fileName: 'AutoDroid-1.2.3.dmg',
    version: '1.2.3',
  })

  // 回归：1.0.2 每个进度事件都新开一个通知，叠出一屏「正在下载」
  assert.equal(notifications.length, 1, '整个下载只开一个进度通知')
  const [progress] = notifications
  assert.equal(progress.title, '下载更新')
  assert.equal(progress.text(), '正在下载 1.2.3：80%', '进度原地更新到最新百分比')
  assert.deepEqual(progressTexts, ['正在下载 1.2.3：37%', '正在下载 1.2.3：80%'])
  assert.equal(progress.duration, 0, 'progress notice stays until the terminal phase')
  assert.equal(progress.closed, true, 'progress notice is closed on completion')
  assert.deepEqual(notices.filter(([kind]) => kind === 'success').map(([, text]) => text), ['安装完成后请重启应用'])
  assert.equal(desktop.state.unsubscribed, true)
})

test('「不再提示」records the version and leaves the shell untouched', async () => {
  const desktop = fakeDesktop()
  const { useAppUpdate, confirms, notices, localStorage } = updateSetup({ clientMode: true, desktop, listItems: [PKG], confirm: 'cancel' })
  await useAppUpdate().startUpdateCheck()
  assert.equal(confirms.length, 1)
  assert.deepEqual(desktop.state.installCalls, [], 'no download before the user opts in')
  assert.equal(localStorage.store[SKIPPED_UPDATE_KEY], '1.2.3', '被忽略的版本落 localStorage')
  assert.ok(notices.some(([kind, text]) => kind === 'info' && text === '已忽略版本 1.2.3，该版本不再提示'))
})

test('an ignored version stays quiet on startup but a manual check still asks', async () => {
  const desktop = fakeDesktop()
  const { useAppUpdate, confirms, apiCalls, notices } = updateSetup({
    clientMode: true,
    desktop,
    listItems: [PKG],
    skippedVersion: '1.2.3',
  })
  const app = useAppUpdate()
  await app.initSilentUpdateCheck()
  assert.deepEqual(confirms, [], '启动自动检查对已忽略版本不再弹窗')
  assert.deepEqual(notices, [], '也不打扰用户')
  assert.equal(apiCalls.length, 1, '仍然照常查一次版本（新版本仍会提示）')

  await app.startUpdateCheck()
  assert.equal(confirms.length, 1, '手动点检查更新是用户主动问的，照常弹窗')
})

test('an ignored older version never blocks a newer one', async () => {
  const desktop = fakeDesktop()
  const { useAppUpdate, confirms } = updateSetup({
    clientMode: true,
    desktop,
    listItems: [{ ...PKG, version_name: '1.3.0' }],
    skippedVersion: '1.2.3',
  })
  await useAppUpdate().initSilentUpdateCheck()
  assert.equal(confirms.length, 1)
  assert.match(confirms[0][0], /新版本 <strong>1\.3\.0<\/strong>/)
})

test('closing the dialog (X / Esc) neither installs nor records a skip', async () => {
  const desktop = fakeDesktop()
  const first = updateSetup({ clientMode: true, desktop, listItems: [PKG], confirm: 'close' })
  await first.useAppUpdate().startUpdateCheck()
  assert.deepEqual(desktop.state.installCalls, [])
  assert.equal(first.localStorage.store[SKIPPED_UPDATE_KEY], undefined, '直接关掉不算「不再提示」')

  const second = updateSetup({ clientMode: true, desktop: fakeDesktop(), listItems: [PKG] })
  await second.useAppUpdate().initSilentUpdateCheck()
  assert.equal(second.confirms.length, 1, '下次启动照常提示')
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
  assert.equal(notifications.length, 1, '失败路径同样只有一个进度通知')
  assert.equal(notifications[0].closed, true, 'progress notice closes on failure')
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
  assert.match(second.confirms[0][0], /新版本 <strong>1\.2\.3<\/strong>/, 'tray opens the same update dialog')

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

// ==================== 内置更新：待重启复用 / 双源回退提示 ====================

test('a staged update offers restart directly without re-downloading', async () => {
  const desktop = fakeDesktop({
    clientInfo: { platform: 'darwin', version: '1.0.0' },
    staged: { version: '1.3.0', fileName: 'AutoDroid-1.3.0-arm64.dmg', size: 251433097 },
  })
  const { useAppUpdate, apiCalls, confirms } = updateSetup({ clientMode: true, desktop, listItems: [] })
  const app = useAppUpdate()
  await app.startUpdateCheck()

  assert.deepEqual(apiCalls, [], '已暂存待重启时不再查询版本源')
  assert.equal(confirms.length, 1)
  assert.equal(confirms[0][1], '重启完成更新')
  assert.match(confirms[0][0], /1\.3\.0/)
  assert.equal(desktop.state.applyCalls, 1, '确认后直接换包重启')
})

test('「立即更新」restarts the app right after the download stages, without asking twice', async () => {
  const desktop = fakeDesktop({
    events: [{ phase: 'downloading', percent: 100 }, { phase: 'ready', version: '1.2.3', path: '/updates/AutoDroid-1.2.3.dmg' }],
    clientInfo: { platform: 'darwin', version: '1.0.0' },
  })
  const { useAppUpdate, confirms, notices } = updateSetup({ clientMode: true, desktop, listItems: [PKG] })
  await useAppUpdate().startUpdateCheck()

  assert.equal(confirms.length, 1, '只有更新弹窗这一个确认，不再追问重启')
  assert.equal(desktop.state.applyCalls, 1, '下载完成即换包重启')
  assert.ok(notices.some(([kind, text]) => kind === 'success' && text === '更新已下载，正在重启完成安装'))
})

test('a failed auto-restart degrades to a retryable error instead of a dead end', async () => {
  const desktop = fakeDesktop({
    events: [{ phase: 'ready', version: '1.2.3', path: '/updates/AutoDroid-1.2.3.dmg' }],
    clientInfo: { platform: 'darwin', version: '1.0.0' },
    applyError: '磁盘空间不足',
  })
  const { useAppUpdate, notices } = updateSetup({ clientMode: true, desktop, listItems: [PKG] })
  await useAppUpdate().startUpdateCheck()
  assert.ok(
    notices.some(([kind, text]) => kind === 'error' && /安装更新失败：磁盘空间不足/.test(text)),
    '换包失败要给出可重试的提示',
  )
})

test('a staged update asks to restart in the same dialog on startup', async () => {
  const desktop = fakeDesktop({
    clientInfo: { platform: 'darwin', version: '1.0.0' },
    staged: { version: '1.3.0', fileName: 'AutoDroid-1.3.0-arm64.dmg', size: 10 },
  })
  const { useAppUpdate, confirms, notices, notifications } = updateSetup({ clientMode: true, desktop, listItems: [] })
  const app = useAppUpdate()
  await app.initSilentUpdateCheck()

  assert.deepEqual(notifications, [], '启动不再只弹通知，直接给弹窗')
  assert.equal(confirms.length, 1)
  assert.equal(confirms[0][1], '重启完成更新')
  assert.deepEqual(notices, [], '静默检查不弹 message')
  assert.equal(desktop.state.applyCalls, 1, '点「立即重启」直接换包重启，不再重新下载')

  const declined = fakeDesktop({
    clientInfo: { platform: 'darwin', version: '1.0.0' },
    staged: { version: '1.3.0', fileName: 'AutoDroid-1.3.0-arm64.dmg', size: 10 },
  })
  const later = updateSetup({ clientMode: true, desktop: declined, listItems: [], confirm: 'cancel' })
  await later.useAppUpdate().initSilentUpdateCheck()
  assert.equal(later.confirms.length, 1)
  assert.equal(declined.state.applyCalls, 0, '选「稍后」就不动应用，下次启动再问')
})

test('an older staged artifact falls back to the normal version check', async () => {
  const desktop = fakeDesktop({
    clientInfo: { platform: 'darwin', version: '2.0.0' },
    staged: { version: '1.3.0', fileName: 'AutoDroid-1.3.0-arm64.dmg', size: 10 },
  })
  const { useAppUpdate, apiCalls, notices } = updateSetup({ clientMode: true, desktop, listItems: [PKG] })
  const app = useAppUpdate()
  await app.startUpdateCheck()

  assert.deepEqual(apiCalls, [['getPackages', { package_name: 'autodroid-desktop', latest_only: true }]], '退回常规检查')
  assert.deepEqual(notices.map(item => item[1]), ['已是最新版本（2.0.0）'])
  assert.equal(desktop.state.applyCalls, 0)
})

test('a failing primary source surfaces the fallback notice and finishes on the backup source', async () => {
  const release = {
    version: '1.2.3',
    fileName: 'AutoDroid-1.2.3-arm64.dmg',
    url: 'https://github.com/liqiu-debug/AutoDroid-Desktop/releases/download/v1.2.3/AutoDroid-1.2.3-arm64.dmg',
    size: 251433097,
    pageUrl: 'https://github.com/liqiu-debug/AutoDroid-Desktop/releases/tag/v1.2.3',
  }
  const desktop = fakeDesktop({
    release,
    clientInfo: { platform: 'darwin', version: '1.0.0' },
    installHandler: (payload, state) => {
      if (state.installCalls.length === 1) throw new Error('下载不完整（1024/2048 字节）')
      state.subscriber?.({ phase: 'done', path: '/updates/AutoDroid-1.2.3-arm64.dmg' })
      return { launched: true, path: '/updates/AutoDroid-1.2.3-arm64.dmg' }
    },
  })
  const { useAppUpdate, notifications, confirms, progressTexts } = updateSetup({ clientMode: true, desktop, listItems: [PKG] })
  const app = useAppUpdate()
  await app.startUpdateCheck()

  assert.equal(confirms[0][1], '发现新版本')
  assert.equal(desktop.state.installCalls.length, 2, '主源失败自动切备用源')
  assert.equal(desktop.state.installCalls[1].url, release.url)
  assert.equal(notifications.length, 1, '切换备用源也复用同一个进度通知')
  assert.ok(progressTexts.some(text => /正在改用备用源/.test(text)), '回退时提示主源不可用')
})
