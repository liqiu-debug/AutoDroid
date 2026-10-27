/**
 * 桌面客户端「检查更新」流程（纯逻辑，无 vue / element-plus / axios 依赖）。
 *
 * 跨端契约（与平台「App 包管理」、桌面壳 preload 对齐）：
 * - 版本查询：GET /api/packages/?package_name=autodroid-desktop&latest_only=true，
 *   客户端按自身平台选条目（window.desktop.getClientInfo().platform：darwin→macos、
 *   win32→windows）；列表条目含 id/platform/version_name/file_size。
 * - 下载 URL：/api/packages/{id}/download?token=<localStorage token>，
 *   拼成 http(s) 绝对地址交给壳（壳只允许 http(s) 且不存储 token，URL 现拼现用）。
 * - 安装：window.desktop.installUpdate({ url, fileName, version })，
 *   过度事件经 window.desktop.onUpdateProgress(cb)（订阅式，返回退订函数）：
 *   {phase:'downloading', percent} | {phase:'done', path} | {phase:'error', message}。
 *
 * 本模块把壳的「事件 + Promise」双通道归一为：
 * - onPhase 回调收到 0..N 次 downloading、且恰好一次终态（done 或 error）；
 * - 返回的 Promise 与终态一致（done → resolve {launched:true}，error → reject Error(message)）。
 */
import { compareVersions } from './semver.js'

/** 更新源查询用的固定包名（桌面包上传时写入的 package_name）。 */
export const UPDATE_PACKAGE_NAME = 'autodroid-desktop'

/** 桌面壳 getClientInfo().platform（Electron process.platform）→ 平台侧 platform 取值。 */
const DESKTOP_PLATFORM_MAP = {
  darwin: 'macos',
  mac: 'macos',
  macos: 'macos',
  win32: 'windows',
  win: 'windows',
  windows: 'windows',
}

/** 桌面通道仅放行的安装包扩展（平台侧约定）。 */
const DESKTOP_EXTENSIONS = ['exe', 'msi', 'dmg']

/** 平台标识映射：darwin→macos、win32→windows；其余（如 linux / 纯 Web）返回 null。 */
export function mapDesktopPlatform(platform) {
  const key = String(platform ?? '').trim().toLowerCase()
  return DESKTOP_PLATFORM_MAP[key] || null
}

/** 按平台选桌面包条目（platform 已是 windows|macos 形态）。 */
export function pickDesktopPackage(items, platform) {
  if (!platform) return null
  const list = Array.isArray(items) ? items : []
  const wanted = String(platform).trim().toLowerCase()
  return list.find(item => String(item?.platform ?? '').trim().toLowerCase() === wanted) || null
}

/** 下载 URL：/api/packages/{id}/download?token=<token>，基于 origin 拼成绝对地址。 */
export function buildDownloadUrl(pkgId, token, origin) {
  const base = String(origin ?? '').trim().replace(/\/+$/, '')
  const id = encodeURIComponent(String(pkgId ?? ''))
  const query = encodeURIComponent(String(token ?? ''))
  return `${base}/api/packages/${id}/download?token=${query}`
}

/**
 * 壳落盘用文件名（userData/updates/<fileName>，拉起方式按扩展分支）。
 * 扩展优先取 file_path 原始扩展（.exe/.msi/.dmg），否则按平台默认（windows→.exe、macos→.dmg）。
 */
export function buildInstallerFileName(pkg) {
  const platform = String(pkg?.platform ?? '').trim().toLowerCase()
  const pathMatch = /\.([A-Za-z0-9]+)$/.exec(String(pkg?.file_path ?? ''))
  const pathExt = pathMatch ? pathMatch[1].toLowerCase() : ''
  const extension = DESKTOP_EXTENSIONS.includes(pathExt)
    ? pathExt
    : (platform === 'macos' ? 'dmg' : 'exe')
  const version = String(pkg?.version_name ?? '')
    .replace(/[^\w.\-]/g, '')
    .replace(/\.{2,}/g, '.')
    .replace(/^\.+|\.+$/g, '')
    || 'setup'
  return `AutoDroid-${version}.${extension}`
}

/**
 * 检查更新：查包列表 → 按平台选条目 → 仅当远端 > 本地才算有新版。
 *
 * @param {object} deps
 * @param {() => Promise<Array<object>>} deps.listPackages 返回桌面包条目列表
 * @param {() => object|null} deps.getBridge 返回 window.desktop（缺失为 null）
 * @param {(a: string, b: string) => number} [deps.compare] 版本比较，默认 compareVersions
 * @returns {Promise<object>} 见下：
 *   {supported:false, reason:'no-bridge'|'unsupported-platform'|'no-version'}
 *   {supported:true, hasUpdate:false, reason:'no-package', localVersion}
 *   {supported:true, hasUpdate:false, localVersion, remoteVersion}
 *   {supported:true, hasUpdate:true, localVersion, remoteVersion, pkg}
 */
export async function checkForUpdate({ listPackages, getBridge, compare = compareVersions } = {}) {
  const bridge = typeof getBridge === 'function' ? getBridge() : null
  // preload 的 getClientInfo 走 ipcRenderer.invoke，返回 Promise（形状 {platform, version}）；
  // 用 await 归一（同步返回也兼容），避免把 Promise 当 clientInfo 读到 undefined 平台。
  let clientInfo = null
  if (typeof bridge?.getClientInfo === 'function') {
    try {
      clientInfo = await bridge.getClientInfo()
    } catch (error) {
      clientInfo = null
    }
  }
  if (!clientInfo) return { supported: false, reason: 'no-bridge' }
  const platform = mapDesktopPlatform(clientInfo.platform)
  if (!platform) return { supported: false, reason: 'unsupported-platform' }
  const localVersion = String(clientInfo.version ?? '').trim()
  if (!localVersion) return { supported: false, reason: 'no-version' }

  const items = typeof listPackages === 'function' ? await listPackages() : []
  const pkg = pickDesktopPackage(items, platform)
  if (!pkg) return { supported: true, hasUpdate: false, reason: 'no-package', localVersion }

  const remoteVersion = String(pkg.version_name ?? '').trim()
  const hasUpdate = compare(remoteVersion, localVersion) > 0
  return { supported: true, hasUpdate, localVersion, remoteVersion, pkg }
}

/**
 * 下载并拉起安装器（installUpdate + onUpdateProgress 归一）。
 *
 * @param {object} deps
 * @param {object} deps.pkg 包条目（id/platform/version_name/file_path）
 * @param {string} deps.token localStorage token（现拼进 URL，壳不存储）
 * @param {string} deps.origin 页面 origin（http(s) 绝对地址前缀）
 * @param {object} deps.bridge window.desktop
 * @param {(evt: {phase:string, percent?:number, path?:string, message?:string}) => void} [deps.onPhase]
 * @returns {Promise<{launched: true}>} 成功 resolve；失败 reject Error(message)
 */
export async function runUpdateInstall({ pkg, token, origin, bridge, onPhase } = {}) {
  const emit = evt => {
    if (typeof onPhase === 'function') onPhase(evt)
  }

  let terminal = null
  const settle = evt => {
    if (terminal) return
    terminal = evt
    emit(evt)
  }
  const fail = message => {
    settle({ phase: 'error', message })
    return new Error(message)
  }

  const url = buildDownloadUrl(pkg?.id, token, origin)
  if (!/^https?:\/\//i.test(url)) {
    throw fail('更新下载地址无效')
  }
  if (!bridge?.installUpdate) {
    throw fail('当前环境不支持安装更新')
  }

  const fileName = buildInstallerFileName(pkg)
  const version = String(pkg?.version_name ?? '').trim()

  let unsubscribe = () => {}
  try {
    if (typeof bridge.onUpdateProgress === 'function') {
      const maybeUnsubscribe = bridge.onUpdateProgress(evt => {
        if (!evt || typeof evt !== 'object') return
        if (evt.phase === 'downloading') {
          const percent = Number(evt.percent)
          emit({
            phase: 'downloading',
            percent: Number.isFinite(percent) ? Math.min(100, Math.max(0, Math.round(percent))) : 0,
          })
        } else if (evt.phase === 'done') {
          settle({ phase: 'done', path: String(evt.path ?? '') })
        } else if (evt.phase === 'error') {
          settle({ phase: 'error', message: String(evt.message ?? '下载更新失败') })
        }
      })
      if (typeof maybeUnsubscribe === 'function') unsubscribe = maybeUnsubscribe
    }

    const result = await bridge.installUpdate({ url, fileName, version })
    if (terminal?.phase === 'error') throw new Error(terminal.message)
    settle({ phase: 'done', path: String(result?.path ?? '') })
    return { launched: true }
  } catch (err) {
    const message = String(err?.message ?? err ?? '下载更新失败')
    if (terminal?.phase !== 'done') settle({ phase: 'error', message })
    throw new Error(message)
  } finally {
    unsubscribe()
  }
}
