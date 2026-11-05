import assert from 'node:assert/strict'
import test from 'node:test'

import {
  EMPTY_UPDATE_NOTES,
  SKIPPED_UPDATE_KEY,
  UPDATE_PACKAGE_NAME,
  applyUpdate,
  buildDownloadUrl,
  buildInstallerFileName,
  buildUpdateDialogHtml,
  checkForUpdate,
  getStagedUpdate,
  isUpdateSkipped,
  mapDesktopPlatform,
  normalizeUpdateNotes,
  pickDesktopPackage,
  readSkippedUpdateVersion,
  rememberSkippedUpdateVersion,
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
function fakeBridge({ events = [], settle = { type: 'resolve' }, clientInfo = { platform: 'darwin', version: '1.0.0' }, release = null, installHandler = null } = {}) {
  const state = { installCalls: [], unsubscribed: false, subscriber: null, releaseQueries: 0 }
  return {
    state,
    getClientInfo: async () => clientInfo,
    onUpdateProgress: cb => {
      state.subscriber = cb
      return () => { state.unsubscribed = true }
    },
    checkRelease: async () => {
      state.releaseQueries += 1
      if (!release) return { ok: false, message: '更新源不可达' }
      return { ok: true, release }
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
  }
}

/** Release 源信息（壳内 GitHub 查询结果）。 */
const releaseInfo = (overrides = {}) => ({
  version: '1.2.3',
  fileName: 'AutoDroid-1.2.3-arm64.dmg',
  url: 'https://github.com/liqiu-debug/AutoDroid-Desktop/releases/download/v1.2.3/AutoDroid-1.2.3-arm64.dmg',
  size: 251433097,
  pageUrl: 'https://github.com/liqiu-debug/AutoDroid-Desktop/releases/tag/v1.2.3',
  ...overrides,
})

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

// ==================== 双源（GitHub Release + 平台包管理）契约 ====================

test('checkForUpdate takes the higher version across release and package sources', async () => {
  // Release 更新 → 以 Release 为准
  const releaseWins = await checkForUpdate({
    listPackages: async () => [desktopPackage({ version_name: '1.5.0' })],
    getBridge: () => fakeBridge({ release: releaseInfo({ version: '2.0.0' }) }),
  })
  assert.equal(releaseWins.hasUpdate, true)
  assert.equal(releaseWins.remoteVersion, '2.0.0')
  assert.equal(releaseWins.source, 'release')
  assert.equal(releaseWins.release?.url, releaseInfo({ version: '2.0.0' }).url)
  assert.equal(releaseWins.pkg, null, '版本不一致的包条目不得作为回退源')

  // 平台包更新（忘记发 Release）→ 以包管理为准
  const packageWins = await checkForUpdate({
    listPackages: async () => [desktopPackage({ version_name: '1.5.0' })],
    getBridge: () => fakeBridge({ release: releaseInfo({ version: '1.0.1' }) }),
  })
  assert.equal(packageWins.remoteVersion, '1.5.0')
  assert.equal(packageWins.source, 'package')
  assert.equal(packageWins.pkg?.version_name, '1.5.0')
  assert.equal(packageWins.release, null)
})

test('checkForUpdate carries both download sources when versions agree', async () => {
  const info = await checkForUpdate({
    listPackages: async () => [desktopPackage({ version_name: '1.2.3' })],
    getBridge: () => fakeBridge({ release: releaseInfo() }),
  })
  assert.equal(info.hasUpdate, true)
  assert.equal(info.remoteVersion, '1.2.3')
  assert.equal(info.source, 'release')
  assert.equal(info.release?.size, 251433097)
  assert.equal(info.pkg?.id, 7, '同版本时包管理条目保留为回退下载源')
})

test('checkForUpdate still works when the release source is unreachable', async () => {
  const info = await checkForUpdate({
    listPackages: async () => [desktopPackage({ version_name: '2.0.0' })],
    getBridge: () => fakeBridge({ release: null }),  // checkRelease 返回 ok:false
  })
  assert.equal(info.hasUpdate, true)
  assert.equal(info.source, 'package')
  assert.equal(info.release, null)
})

test('runUpdateInstall prefers the package mirror and falls back to the release asset', async () => {
  // 主源（平台包管理/云端隧道）失败 → 自动切 GitHub 资产，事件串里出现一次 fallback
  const bridge = fakeBridge({
    installHandler: (payload, state) => {
      if (state.installCalls.length === 1) throw new Error('下载不完整（1024/2048 字节）')
      state.subscriber?.({ phase: 'done', path: '/updates/AutoDroid-1.2.3-arm64.dmg' })
      return { launched: true, path: '/updates/AutoDroid-1.2.3-arm64.dmg' }
    },
  })
  const phases = []
  const result = await runUpdateInstall({
    pkg: desktopPackage({ version_name: '1.2.3' }),
    release: releaseInfo(),
    token: 'tok-1',
    origin: ORIGIN,
    bridge,
    onPhase: evt => phases.push(evt),
  })

  assert.deepEqual(result, { launched: true })
  assert.equal(bridge.state.installCalls.length, 2)
  assert.deepEqual(bridge.state.installCalls[0], {
    url: `${ORIGIN}/api/packages/7/download?token=tok-1`,
    fileName: 'AutoDroid-1.2.3.dmg',
    version: '1.2.3',
  })
  assert.deepEqual(bridge.state.installCalls[1], {
    url: releaseInfo().url,
    fileName: 'AutoDroid-1.2.3-arm64.dmg',
    version: '1.2.3',
    expectedSize: 251433097,
  })
  assert.deepEqual(phases.map(evt => evt.phase), ['fallback', 'done'])
  assert.equal(phases[0].from, 'package')
  assert.equal(phases[0].to, 'release')
})

test('runUpdateInstall uses the release asset directly when there is no package entry', async () => {
  const bridge = fakeBridge({
    installHandler: (payload, state) => {
      state.subscriber?.({ phase: 'ready', version: '1.2.3', path: '/updates/x.dmg' })
      return { launched: false, ready: true, version: '1.2.3' }
    },
  })
  const phases = []
  await runUpdateInstall({ release: releaseInfo(), bridge, onPhase: evt => phases.push(evt) })
  assert.equal(bridge.state.installCalls.length, 1)
  assert.equal(bridge.state.installCalls[0].url, releaseInfo().url)
  assert.equal(bridge.state.installCalls[0].expectedSize, 251433097)
  assert.deepEqual(phases.map(evt => evt.phase), ['ready'])
})

test('runUpdateInstall reports both failures with the release page hint', async () => {
  const bridge = fakeBridge({
    installHandler: (payload, state) => {
      if (state.installCalls.length === 1) throw new Error('下载不完整（1/2 字节）')
      throw new Error('查询/下载 GitHub 失败')
    },
  })
  const phases = []
  await assert.rejects(
    () => runUpdateInstall({
      pkg: desktopPackage({ version_name: '1.2.3' }),
      release: releaseInfo(),
      token: 'tok-1',
      origin: ORIGIN,
      bridge,
      onPhase: evt => phases.push(evt),
    }),
    /手动下载/,
  )
  const error = phases.find(evt => evt.phase === 'error')
  assert.ok(error, 'error phase present')
  assert.match(error.message, /平台包管理源 下载不完整/, '主源错误保留在最终提示里')
  assert.match(error.message, /GitHub源 查询\/下载 GitHub 失败/, '备用源错误保留在最终提示里')
  assert.match(error.message, new RegExp(releaseInfo().pageUrl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
})

test('getStagedUpdate / applyUpdate degrade without the bridge capability', async () => {
  assert.equal(await getStagedUpdate(null), null)
  assert.equal(await getStagedUpdate({}), null)
  assert.deepEqual(
    await getStagedUpdate({ getUpdateStatus: async () => ({ staged: { version: '1.2.3', fileName: 'x.dmg', size: 12 } }) }),
    { version: '1.2.3', fileName: 'x.dmg', size: 12 },
  )
  assert.equal(await getStagedUpdate({ getUpdateStatus: async () => ({ staged: null }) }), null)

  await assert.rejects(() => applyUpdate({ bridge: null }), /不支持自动重启更新/)
  assert.equal(await applyUpdate({ bridge: { applyUpdate: async () => ({ ok: true }) } }), true)
  await assert.rejects(
    () => applyUpdate({ bridge: { applyUpdate: async () => ({ ok: false, message: '没有待安装的更新' }) } }),
    /没有待安装的更新/,
  )
})

// ==================== 更新提示（changelog）与「不再提示」 ====================

test('the update notes come from the winning source: package changelog or release body', async () => {
  // 包管理源胜出 → 用上传时手写的 changelog
  const fromPackage = await checkForUpdate({
    listPackages: async () => [desktopPackage({ changelog: '修复录制断连' })],
    getBridge: () => fakeBridge(),
  })
  assert.equal(fromPackage.remoteVersion, '1.2.3')
  assert.equal(fromPackage.changelog, '修复录制断连')

  // Release 版本更高而胜出 → 用该 Release 的正文（notes）
  const fromRelease = await checkForUpdate({
    listPackages: async () => [desktopPackage({ changelog: '旧包的文案' })],
    getBridge: () => fakeBridge({ release: releaseInfo({ version: '1.3.0', notes: '新增 iOS 稳定性测试' }) }),
  })
  assert.equal(fromRelease.remoteVersion, '1.3.0')
  assert.equal(fromRelease.source, 'release')
  assert.equal(fromRelease.changelog, '新增 iOS 稳定性测试')

  // 同版本时（发版惯例：Release 与平台包各传一份）优先用发版人手写在平台的那份
  const sameVersionBothSources = await checkForUpdate({
    listPackages: async () => [desktopPackage({ changelog: '平台写的更新提示' })],
    getBridge: () => fakeBridge({ release: releaseInfo({ notes: 'GitHub 正文' }) }),
  })
  assert.equal(sameVersionBothSources.source, 'release', '同版本仍优先 Release 的下载元数据')
  assert.equal(sameVersionBothSources.changelog, '平台写的更新提示')
  assert.equal(sameVersionBothSources.pkg?.id, 7, '同版本的平台包仍作为下载候选')

  // 两源都没有文案 → 空串，由展示层给兜底文案
  const noNotes = await checkForUpdate({
    listPackages: async () => [desktopPackage()],
    getBridge: () => fakeBridge(),
  })
  assert.equal(noNotes.changelog, '')
})

test('normalizeUpdateNotes keeps intentional line breaks and fills the empty case', () => {
  assert.equal(normalizeUpdateNotes('  修复 A\r\n新增 B  '), '修复 A\n新增 B')
  assert.equal(normalizeUpdateNotes('修复 A\n\n\n\n新增 B'), '修复 A\n\n新增 B')
  assert.equal(normalizeUpdateNotes(''), EMPTY_UPDATE_NOTES)
  assert.equal(normalizeUpdateNotes(null), EMPTY_UPDATE_NOTES)
  assert.equal(normalizeUpdateNotes('   '), EMPTY_UPDATE_NOTES)
})

test('the dialog html carries the version pair and an escaped notes block', () => {
  const html = buildUpdateDialogHtml({
    remoteVersion: '1.2.3',
    localVersion: '1.0.0',
    notes: '修复 A\n新增 B',
  })
  assert.match(html, /新版本 <strong>1\.2\.3<\/strong>/)
  assert.match(html, /（当前 1\.0\.0）/)
  assert.match(html, /<span class="autodroid-update-notes">修复 A\n新增 B<\/span>/)
  // ElMessageBox 把这段 HTML 塞进 <p>：不能出现块级标签，否则会被浏览器提前闭合
  assert.doesNotMatch(html, /<(div|p|section|ul|li)\b/)

  const escaped = buildUpdateDialogHtml({ remoteVersion: '<b>1.2.3</b>', notes: '<script>x</script>' })
  assert.doesNotMatch(escaped, /<script>/)
  assert.match(escaped, /&lt;b&gt;1\.2\.3&lt;\/b&gt;/)
  assert.match(escaped, /&lt;script&gt;x&lt;\/script&gt;/)

  const blank = buildUpdateDialogHtml({ remoteVersion: '1.2.3', notes: '' })
  assert.match(blank, new RegExp(EMPTY_UPDATE_NOTES))
})

test('the ignored version is remembered one at a time and only mutes that version', () => {
  const store = new Map()
  const storage = {
    getItem: key => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
  }
  assert.equal(readSkippedUpdateVersion(storage), '')
  assert.equal(isUpdateSkipped('1.2.3', readSkippedUpdateVersion(storage)), false)

  assert.equal(rememberSkippedUpdateVersion(storage, '1.2.3'), true)
  assert.equal(store.get(SKIPPED_UPDATE_KEY), '1.2.3')
  assert.equal(readSkippedUpdateVersion(storage), '1.2.3')
  assert.equal(isUpdateSkipped('1.2.3', '1.2.3'), true)
  assert.equal(isUpdateSkipped('v1.2.3', '1.2.3'), true, 'v 前缀等价')
  assert.equal(isUpdateSkipped('1.2.2', '1.2.3'), true, '比忽略版本更旧也不该弹')
  assert.equal(isUpdateSkipped('1.2.4', '1.2.3'), false, '出现更高版本照常提示')
  assert.equal(isUpdateSkipped('', '1.2.3'), false)

  // 再忽略新版本：只留最近一次，旧记录被覆盖
  rememberSkippedUpdateVersion(storage, '1.3.0')
  assert.equal(readSkippedUpdateVersion(storage), '1.3.0')
  assert.equal(isUpdateSkipped('1.2.3', readSkippedUpdateVersion(storage)), true)

  assert.equal(rememberSkippedUpdateVersion(storage, ''), false, '空版本不写记录')
})

test('missing or throwing storage degrades to "nothing ignored"', () => {
  assert.equal(readSkippedUpdateVersion(null), '')
  assert.equal(rememberSkippedUpdateVersion(null, '1.2.3'), false)
  const throwing = {
    getItem: () => { throw new Error('storage disabled') },
    setItem: () => { throw new Error('storage disabled') },
  }
  assert.equal(readSkippedUpdateVersion(throwing), '')
  assert.equal(rememberSkippedUpdateVersion(throwing, '1.2.3'), false)
})
