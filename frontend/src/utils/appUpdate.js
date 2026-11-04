/**
 * 桌面客户端「检查更新」流程（纯逻辑，无 vue / element-plus / axios 依赖）。
 *
 * 跨端契约：
 * - 版本查询（双源并行，取更高版本；同版本优先 Release）：
 *   1) 壳内查 GitHub Release（公开仓库，window.desktop.checkRelease()）——下载链路
 *      带断点续传，隧道截断可续传，不依赖平台包管理是否有对应条目；
 *   2) 平台「App 包管理」：GET /api/packages/?package_name=autodroid-desktop&latest_only=true，
 *      按自身平台选条目（darwin→macos、win32→windows）。
 * - 下载：Release 源直接下载资产 URL（无需 token）；包管理源现拼
 *   /api/packages/{id}/download?token=<localStorage token> 绝对地址。
 * - 安装：window.desktop.installUpdate({ url, fileName, version, expectedSize? })，
 *   过程事件经 window.desktop.onUpdateProgress(cb)（订阅式，返回退订函数）：
 *   {phase:'downloading', percent} | {phase:'ready', version, path}
 *   | {phase:'downloaded', path, message} | {phase:'done', path} | {phase:'error', message}。
 *   macOS 内置安装完成下载后进入 ready（已暂存），由 applyUpdate() 换包并重启；
 *   win32 为 done（已拉起安装向导）。downloaded = 自动安装不可用，已打开安装包手动安装。
 *
 * 本模块把壳的「事件 + Promise」双通道归一为：
 * - onPhase 回调收到 0..N 次 downloading、且恰好一次终态（ready/downloaded/done/error）；
 * - 返回的 Promise 与终态一致（ready → {ready:true}，其余成功 → {launched:true}，
 *   error → reject Error(message)）。
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
 * 壳内查 GitHub Release（客户端模式才有 checkRelease；缺失/失败返回 null 走包管理源）。
 * @returns {Promise<{source:'release', version:string, fileName:string, url:string, size:number}|null>}
 */
async function queryShellRelease(bridge) {
  if (typeof bridge?.checkRelease !== 'function') return null
  try {
    const result = await bridge.checkRelease()
    if (!result || result.ok !== true || !result.release) return null
    const version = String(result.release.version ?? '').trim()
    const url = String(result.release.url ?? '').trim()
    if (!version || !/^https?:\/\//i.test(url)) return null
    const fileName = String(result.release.fileName ?? '').trim()
    return {
      source: 'release',
      version,
      url,
      size: Number(result.release.size) || 0,
      pageUrl: String(result.release.pageUrl ?? '').trim(),
      fileName: fileName || buildInstallerFileName({ platform: 'macos', version_name: version }),
    }
  } catch (error) {
    return null
  }
}

/**
 * 查询壳内是否已有下载完成的更新（重启入口复用，无需联网/重新下载）。
 * @returns {Promise<{version:string, fileName:string, size:number}|null>}
 */
export async function getStagedUpdate(bridge) {
  if (typeof bridge?.getUpdateStatus !== 'function') return null
  try {
    const result = await bridge.getUpdateStatus()
    const staged = result?.staged
    if (!staged || !staged.version) return null
    return {
      version: String(staged.version),
      fileName: String(staged.fileName ?? ''),
      size: Number(staged.size) || 0,
    }
  } catch (error) {
    return null
  }
}

/**
 * 应用已暂存的更新并重启（macOS）：壳替换应用包后重新启动进程。
 * 成功时进程即将退出，无需后续处理。
 */
export async function applyUpdate({ bridge } = {}) {
  const target = bridge || (typeof window === 'undefined' ? null : (window.desktop || null))
  if (typeof target?.applyUpdate !== 'function') {
    throw new Error('当前环境不支持自动重启更新')
  }
  const result = await target.applyUpdate()
  if (result && result.ok === false) {
    throw new Error(result.message || '安装更新失败')
  }
  return true
}

/**
 * 检查更新：双源并行（壳内 Release + 平台包管理）→ 取版本更高者（同版本优先 Release）
 * → 仅当远端 > 本地才算有新版。
 *
 * @param {object} deps
 * @param {() => Promise<Array<object>>} deps.listPackages 返回桌面包条目列表
 * @param {() => object|null} deps.getBridge 返回 window.desktop（缺失为 null）
 * @param {(a: string, b: string) => number} [deps.compare] 版本比较，默认 compareVersions
 * @returns {Promise<object>} 见下：
 *   {supported:false, reason:'no-bridge'|'unsupported-platform'|'no-version'}
 *   {supported:true, hasUpdate:false, reason:'no-package', localVersion}
 *   {supported:true, hasUpdate:false, localVersion, remoteVersion, source}
 *   {supported:true, hasUpdate:true, localVersion, remoteVersion, source, release?|pkg?}
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

  // 双源并行：壳内 Release 查询失败/缺失 → 只余包管理源（原行为不变）；
  // 平台包查询失败时记下错误，两源都拿不到时按原行为报「检查更新失败」
  let listError = null
  const [release, listed] = await Promise.all([
    queryShellRelease(bridge),
    (async () => (typeof listPackages === 'function' ? await listPackages() : []))().catch((error) => {
      listError = error
      return []
    }),
  ])
  const items = Array.isArray(listed) ? listed : []

  const pkg = pickDesktopPackage(items, platform)
  const packageCandidate = pkg
    ? { source: 'package', version: String(pkg.version_name ?? '').trim(), pkg }
    : null
  const candidates = [release, packageCandidate].filter(candidate => candidate && candidate.version)
  if (!candidates.length) {
    if (listError && !release) throw listError
    return { supported: true, hasUpdate: false, reason: 'no-package', localVersion }
  }
  // 取版本最大者；版本相同时保留前者（Release 排在前：元数据更完整）
  const best = candidates.reduce((acc, cur) => (compare(cur.version, acc.version) > 0 ? cur : acc))
  const hasUpdate = compare(best.version, localVersion) > 0
  // 两个下载源都带回去：只有与目标版本一致的源才能作为下载/回退候选
  const sameVersion = candidate => candidate && compare(candidate.version, best.version) === 0
  return {
    supported: true,
    hasUpdate,
    localVersion,
    remoteVersion: best.version,
    source: best.source,
    release: sameVersion(release) ? release : null,
    pkg: sameVersion(packageCandidate) ? packageCandidate.pkg : null,
  }
}

/**
 * 下载并安装更新（installUpdate + onUpdateProgress 归一）。
 *
 * 双源按序尝试：**平台包管理（云端隧道，无需 VPN 即可达）优先，GitHub Release 兜底**；
 * 主源失败自动切备用源（事件 {phase:'fallback', from, to, message}），
 * 全部失败时错误信息附带 Release 页面地址供浏览器手动下载。
 *
 * @param {object} deps
 * @param {object} [deps.pkg] 包管理源条目（id/platform/version_name/file_path）
 * @param {object} [deps.release] Release 源信息（{version, fileName, url, size, pageUrl}）
 * @param {string} deps.token localStorage token（现拼进 URL，壳不存储）
 * @param {string} deps.origin 页面 origin（http(s) 绝对地址前缀）
 * @param {object} deps.bridge window.desktop
 * @param {(evt: {phase:string, percent?:number, path?:string, message?:string, version?:string, from?:string, to?:string}) => void} [deps.onPhase]
 * @returns {Promise<{launched?: true, ready?: true, manual?: true}>} 失败 reject Error(message)
 */
export async function runUpdateInstall({ pkg, release, token, origin, bridge, onPhase } = {}) {
  const emit = evt => {
    if (typeof onPhase === 'function') onPhase(evt)
  }

  if (!bridge?.installUpdate) {
    const message = '当前环境不支持安装更新'
    emit({ phase: 'error', message })
    throw new Error(message)
  }

  const version = String((release?.version ?? pkg?.version_name) ?? '').trim()
  const sources = []
  if (pkg?.id != null) {
    const packageUrl = buildDownloadUrl(pkg.id, token, origin)
    if (/^https?:\/\//i.test(packageUrl)) {
      sources.push({
        kind: 'package',
        url: packageUrl,
        fileName: buildInstallerFileName(pkg),
        expectedSize: 0,
      })
    }
  }
  if (release && /^https?:\/\//i.test(String(release.url ?? ''))) {
    sources.push({
      kind: 'release',
      url: String(release.url),
      fileName: String(release.fileName || buildInstallerFileName({ platform: 'macos', version_name: release.version })),
      expectedSize: Number(release.size) || 0,
    })
  }
  if (!sources.length) {
    const message = '更新下载地址无效'
    emit({ phase: 'error', message })
    throw new Error(message)
  }

  let lastError = null
  const failures = []
  for (let index = 0; index < sources.length; index += 1) {
    const source = sources[index]
    try {
      const result = await installFromSource({ source, version, bridge, emit })
      if (result.ready) {
        emit({ phase: 'ready', version, path: result.path })
        return { ready: true }
      }
      if (result.manual) {
        emit({ phase: 'downloaded', path: result.path, message: result.message })
        return { launched: true, manual: true }
      }
      emit({ phase: 'done', path: result.path })
      return { launched: true }
    } catch (err) {
      lastError = err
      failures.push({ kind: source.kind, message: String(err?.message ?? err ?? '下载失败') })
      const hasNext = index < sources.length - 1
      if (hasNext) {
        emit({ phase: 'fallback', from: source.kind, to: sources[index + 1].kind, message: String(err?.message ?? err ?? '') })
      }
    }
  }

  const labels = { package: '平台包管理', release: 'GitHub' }
  const detail = failures.map(item => `${labels[item.kind] || item.kind}源 ${item.message}`).join('；')
  const hint = release?.pageUrl ? `。可在浏览器打开 ${release.pageUrl} 手动下载` : ''
  // 单源失败沿用原提示（不加包装）；双源都失败时逐源列出，便于定位是哪一侧不通
  const message = failures.length <= 1
    ? `${failures[0]?.message || String(lastError?.message ?? '未知错误')}${hint}`
    : `下载失败（${detail}）${hint}`
  emit({ phase: 'error', message })
  throw new Error(message)
}

/** 单源安装：订阅壳事件 → installUpdate → 归一为 {ready|manual|done} 或抛错。 */
async function installFromSource({ source, version, bridge, emit }) {
  let terminal = null
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
        } else if (evt.phase === 'ready') {
          terminal = { phase: 'ready', version: String(evt.version ?? version), path: String(evt.path ?? '') }
        } else if (evt.phase === 'downloaded') {
          terminal = { phase: 'downloaded', path: String(evt.path ?? ''), message: String(evt.message ?? '') }
        } else if (evt.phase === 'done') {
          terminal = { phase: 'done', path: String(evt.path ?? '') }
        } else if (evt.phase === 'error') {
          terminal = { phase: 'error', message: String(evt.message ?? '下载更新失败') }
        }
      })
      if (typeof maybeUnsubscribe === 'function') unsubscribe = maybeUnsubscribe
    }

    const payload = { url: source.url, fileName: source.fileName, version }
    if (source.expectedSize > 0) payload.expectedSize = source.expectedSize
    const result = await bridge.installUpdate(payload)
    if (terminal?.phase === 'error') throw new Error(terminal.message)
    if (result?.ready === true || terminal?.phase === 'ready') {
      return { ready: true, path: String(result?.path ?? terminal?.path ?? '') }
    }
    if (terminal?.phase === 'downloaded') {
      return { manual: true, path: terminal.path, message: terminal.message }
    }
    return { done: true, path: String(result?.path ?? terminal?.path ?? '') }
  } finally {
    unsubscribe()
  }
}
