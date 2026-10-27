import assert from 'node:assert/strict'
import test from 'node:test'

import {
  UPDATE_PACKAGE_NAME,
  buildDownloadUrl,
  buildInstallerFileName,
  checkForUpdate,
  mapDesktopPlatform,
  pickDesktopPackage,
  runUpdateInstall,
} from '../src/utils/appUpdate.js'

// 桌面端「检查更新」纯流程契约（与平台「App 包管理」、桌面壳 preload 对齐）：
// 查包列表 → 按平台（darwin→macos、win32→windows）选条目 → compareVersions 仅远端>本地
// 才有新版；下载 URL 现拼 token 交壳 installUpdate({url,fileName,version})，
// 事件 + Promise 双通道归一为「downloading* + 恰好一次 done/error」。
// 与仓库既有测试一致，node --test 直接导入纯工具模块（非 vitest）。

const ORIGIN = 'http://127.0.0.1:5800'

const desktopPackage = (overrides = {}) => ({
  id: 7,
  platform: 'macos',
  package_name: UPDATE_PACKAGE_NAME,
  app_name: 'AutoDroid',
  version_name: '1.2.3',
  file_size: 52.4,
  file_path: 'uploads/apps/ab12cd34.dmg',
  ...overrides,
})

/** 假壳桥：installUpdate 期间回放 events，再按 settle 结果收尾。 */
function fakeBridge({ events = [], settle = { type: 'resolve' }, clientInfo = { platform: 'darwin', version: '1.0.0' } } = {}) {
  const state = { installCalls: [], unsubscribed: false, subscriber: null }
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
  }
}

test('mapDesktopPlatform maps Electron platforms and rejects the rest', () => {
  assert.equal(mapDesktopPlatform('darwin'), 'macos')
  assert.equal(mapDesktopPlatform('win32'), 'windows')
  assert.equal(mapDesktopPlatform('macos'), 'macos')
  assert.equal(mapDesktopPlatform('windows'), 'windows')
  assert.equal(mapDesktopPlatform('linux'), null)
  assert.equal(mapDesktopPlatform(''), null)
  assert.equal(mapDesktopPlatform(undefined), null)
})

test('pickDesktopPackage selects the entry of its own platform only', () => {
  const items = [desktopPackage({ id: 1, platform: 'windows' }), desktopPackage({ id: 2, platform: 'macos' })]
  assert.equal(pickDesktopPackage(items, 'macos')?.id, 2)
  assert.equal(pickDesktopPackage(items, 'windows')?.id, 1)
  assert.equal(pickDesktopPackage(items, 'android'), null)
  assert.equal(pickDesktopPackage([], 'macos'), null)
})

test('checkForUpdate degrades without the desktop bridge', async () => {
  const noBridge = await checkForUpdate({
    listPackages: async () => [desktopPackage()],
    getBridge: () => null,
  })
  assert.deepEqual(noBridge, { supported: false, reason: 'no-bridge' })

  const noClientInfo = await checkForUpdate({
    listPackages: async () => [desktopPackage()],
    getBridge: () => ({ installUpdate: async () => ({}) }),
  })
  assert.deepEqual(noClientInfo, { supported: false, reason: 'no-bridge' })
})

test('checkForUpdate rejects unsupported platforms and missing local versions', async () => {
  const linux = await checkForUpdate({
    listPackages: async () => [desktopPackage()],
    getBridge: () => ({ getClientInfo: async () => ({ platform: 'linux', version: '1.0.0' }) }),
  })
  assert.deepEqual(linux, { supported: false, reason: 'unsupported-platform' })

  const noVersion = await checkForUpdate({
    listPackages: async () => [desktopPackage()],
    getBridge: () => ({ getClientInfo: async () => ({ platform: 'darwin', version: '' }) }),
  })
  assert.deepEqual(noVersion, { supported: false, reason: 'no-version' })
})

test('checkForUpdate reports no-package when its platform has no desktop package', async () => {
  const result = await checkForUpdate({
    listPackages: async () => [desktopPackage({ id: 1, platform: 'windows' })],
    getBridge: () => ({ getClientInfo: async () => ({ platform: 'darwin', version: '1.0.0' }) }),
  })
  assert.equal(result.supported, true)
  assert.equal(result.hasUpdate, false)
  assert.equal(result.reason, 'no-package')
  assert.equal(result.localVersion, '1.0.0')
})

test('checkForUpdate awaits the real Promise-based preload getClientInfo', async () => {
  // 真实 window.desktop.getClientInfo() 走 ipcRenderer.invoke，返回 Promise<{platform,version}>；
  // 不 await 会把 Promise 当 clientInfo 读出 undefined 平台，永远判「不支持」。
  const asyncBridge = () => ({
    getClientInfo: async () => ({ platform: 'darwin', version: '1.0.0' }),
    installUpdate: async () => ({ launched: true }),
  })
  const result = await checkForUpdate({
    listPackages: async () => [desktopPackage({ platform: 'macos', version_name: '2.0.0' })],
    getBridge: asyncBridge,
  })
  assert.equal(result.supported, true)
  assert.equal(result.hasUpdate, true)
  assert.equal(result.localVersion, '1.0.0')
  assert.equal(result.remoteVersion, '2.0.0')

  const rejecting = await checkForUpdate({
    listPackages: async () => [desktopPackage()],
    getBridge: () => ({ getClientInfo: async () => { throw new Error('IPC 失败') } }),
  })
  assert.deepEqual(rejecting, { supported: false, reason: 'no-bridge' })
})

test('checkForUpdate prompts only when the remote version is strictly newer', async () => {
  const list = remote => async () => [desktopPackage({ platform: 'windows', version_name: remote })]
  const bridge = () => ({ getClientInfo: async () => ({ platform: 'win32', version: '1.2.3' }) })

  const same = await checkForUpdate({ listPackages: list('1.2.3'), getBridge: bridge })
  assert.equal(same.hasUpdate, false)
  assert.equal(same.remoteVersion, '1.2.3')

  const older = await checkForUpdate({ listPackages: list('1.2.2'), getBridge: bridge })
  assert.equal(older.hasUpdate, false)

  const newer = await checkForUpdate({ listPackages: list('v1.3.0'), getBridge: bridge })
  assert.equal(newer.hasUpdate, true)
  assert.equal(newer.remoteVersion, 'v1.3.0')
  assert.equal(newer.localVersion, '1.2.3')
  assert.equal(newer.pkg.package_name, UPDATE_PACKAGE_NAME)
})

test('buildDownloadUrl appends the query token onto the packages download endpoint', () => {
  assert.equal(
    buildDownloadUrl(7, 'tok-1', ORIGIN),
    `${ORIGIN}/api/packages/7/download?token=tok-1`,
  )
  assert.equal(
    buildDownloadUrl(12, 'a b/c', 'http://127.0.0.1:5800/'),
    'http://127.0.0.1:5800/api/packages/12/download?token=a%20b%2Fc',
  )
})

test('buildInstallerFileName keeps the real installer extension and falls back per platform', () => {
  assert.equal(buildInstallerFileName({ platform: 'windows', version_name: '1.2.3', file_path: 'uploads/apps/x.msi' }), 'AutoDroid-1.2.3.msi')
  assert.equal(buildInstallerFileName({ platform: 'macos', version_name: '1.2.3', file_path: 'uploads/apps/x.dmg' }), 'AutoDroid-1.2.3.dmg')
  assert.equal(buildInstallerFileName({ platform: 'windows', version_name: '1.2.3' }), 'AutoDroid-1.2.3.exe')
  assert.equal(buildInstallerFileName({ platform: 'macos', version_name: '1.2.3' }), 'AutoDroid-1.2.3.dmg')
  assert.equal(buildInstallerFileName({ platform: 'windows', version_name: '1.2.3/../4' }), 'AutoDroid-1.2.3.4.exe')
})

test('runUpdateInstall streams downloading percents and resolves with a single done phase', async () => {
  const bridge = fakeBridge({
    events: [
      { phase: 'downloading', percent: 42.7 },
      { phase: 'downloading', percent: 120 },
      { phase: 'done', path: 'C:/updates/AutoDroid-1.2.3.exe' },
    ],
    settle: { type: 'resolve', value: { launched: true, path: 'C:/updates/AutoDroid-1.2.3.exe' } },
    clientInfo: { platform: 'win32', version: '1.0.0' },
  })
  const phases = []
  const result = await runUpdateInstall({
    pkg: desktopPackage({ id: 7, platform: 'windows', version_name: '1.2.3', file_path: 'uploads/apps/ab.exe' }),
    token: 'tok-1',
    origin: ORIGIN,
    bridge,
    onPhase: evt => phases.push(evt),
  })

  assert.deepEqual(result, { launched: true })
  assert.equal(bridge.state.installCalls.length, 1)
  assert.deepEqual(bridge.state.installCalls[0], {
    url: `${ORIGIN}/api/packages/7/download?token=tok-1`,
    fileName: 'AutoDroid-1.2.3.exe',
    version: '1.2.3',
  })
  assert.deepEqual(phases.map(evt => evt.phase), ['downloading', 'downloading', 'done'])
  assert.equal(phases[0].percent, 43, 'percent is rounded')
  assert.equal(phases[1].percent, 100, 'percent is clamped to 100')
  assert.equal(phases[2].path, 'C:/updates/AutoDroid-1.2.3.exe')
  assert.equal(bridge.state.unsubscribed, true, 'progress subscription is released')
})

test('runUpdateInstall collapses the error event and rejection into one error phase', async () => {
  const bridge = fakeBridge({
    events: [{ phase: 'downloading', percent: 10 }, { phase: 'error', message: '网络中断' }],
    settle: { type: 'reject', message: '网络中断' },
    clientInfo: { platform: 'darwin', version: '1.0.0' },
  })
  const phases = []
  await assert.rejects(
    () => runUpdateInstall({
      pkg: desktopPackage(),
      token: 'tok-1',
      origin: ORIGIN,
      bridge,
      onPhase: evt => phases.push(evt),
    }),
    err => err instanceof Error && err.message === '网络中断',
  )
  assert.deepEqual(phases.map(evt => evt.phase), ['downloading', 'error'], 'exactly one terminal phase')
  assert.equal(bridge.state.unsubscribed, true)
})

test('runUpdateInstall synthesizes the missing terminal phase from the promise outcome', async () => {
  // 壳只 reject 不发 error 事件 → 补一个 error 终态，UI 仍能提示
  const rejectOnly = fakeBridge({ events: [], settle: { type: 'reject', message: '磁盘空间不足' } })
  const errorPhases = []
  await assert.rejects(
    () => runUpdateInstall({ pkg: desktopPackage(), token: 't', origin: ORIGIN, bridge: rejectOnly, onPhase: e => errorPhases.push(e) }),
    /磁盘空间不足/,
  )
  assert.deepEqual(errorPhases, [{ phase: 'error', message: '磁盘空间不足' }])

  // 壳只 resolve 不发 done 事件 → 补一个 done 终态
  const resolveOnly = fakeBridge({ events: [{ phase: 'downloading', percent: 100 }], settle: { type: 'resolve' } })
  const donePhases = []
  const result = await runUpdateInstall({ pkg: desktopPackage(), token: 't', origin: ORIGIN, bridge: resolveOnly, onPhase: e => donePhases.push(e) })
  assert.equal(result.launched, true)
  assert.deepEqual(donePhases.map(e => e.phase), ['downloading', 'done'])
})

test('runUpdateInstall rejects non-http URLs and a missing installUpdate bridge', async () => {
  const phases = []
  await assert.rejects(
    () => runUpdateInstall({ pkg: desktopPackage(), token: 't', origin: 'file:///etc', bridge: fakeBridge(), onPhase: e => phases.push(e) }),
    /更新下载地址无效/,
  )
  await assert.rejects(
    () => runUpdateInstall({ pkg: desktopPackage(), token: 't', origin: ORIGIN, bridge: { onUpdateProgress: () => () => {} }, onPhase: e => phases.push(e) }),
    /当前环境不支持安装更新/,
  )
  assert.deepEqual(phases.map(e => e.phase), ['error', 'error'], 'both refusals surface as error phases')
})
