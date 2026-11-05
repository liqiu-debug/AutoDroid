import { ref } from 'vue'
import { ElMessage, ElMessageBox, ElNotification } from 'element-plus'
import api from '@/api'
import { useClientApp } from '@/composables/useClientApp'
import { compareVersions } from '@/utils/semver'
import {
  applyUpdate,
  buildUpdateDialogHtml,
  checkForUpdate,
  getStagedUpdate,
  isUpdateSkipped,
  readSkippedUpdateVersion,
  rememberSkippedUpdateVersion,
  runUpdateInstall,
} from '@/utils/appUpdate'

/**
 * useAppUpdate - 桌面客户端「检查更新」流程（入口 + 更新弹窗 + 下载进度 + 内置安装重启 + 启动自动检查）。
 *
 * 门控：仅 clientMode（useClientApp 探测）显示入口并执行检查；纯 Web 无入口无检查。
 * 桥缺失（window.desktop 不在）降级为「当前环境不支持检查更新」，不阻塞任何既有业务。
 * 更新源：壳内 GitHub Release + 平台「App 包管理」双源取更高版本。
 * 启动自动检查：clientMode 启动后执行一次，有新版即弹更新弹窗
 * （新版本 + 更新提示 + 「立即更新 / 不再提示」）；点「不再提示」记下该版本，
 * 之后自动检查对该版本保持安静（手动「检查更新」仍会提示，用户是主动问的）。
 * macOS 下载完成即已暂存，确认后换包重启（applyUpdate）；win32 拉起安装向导。
 */

// 下载进度通知固定 id：同 id 通知原地替换，避免进度事件刷屏
const PROGRESS_NOTIFICATION_ID = 'autodroid-update-progress'

// 启动自动检查每会话至多一次（间隔 ≥ 该会话）
let silentCheckStarted = false

export function useAppUpdate() {
  const { isClientApp, detectClientApp } = useClientApp()
  const checking = ref(false)
  const installing = ref(false)
  const downloadPercent = ref(0)

  const getBridge = () => (typeof window === 'undefined' ? null : (window.desktop || null))
  const getToken = () => (typeof localStorage === 'undefined' ? '' : (localStorage.getItem('token') || ''))
  const getOrigin = () => (typeof location === 'undefined' ? '' : (location.origin || ''))
  /** localStorage 兜底：隐私模式/IPC 受限期可能抛错，读写失败只当没记录，不影响更新流程。 */
  const getStorage = () => {
    try {
      return typeof localStorage === 'undefined' ? null : localStorage
    } catch (error) {
      return null
    }
  }

  /** 更新源查询：客户端检查更新自查询形态（平台注释写明）。 */
  const listDesktopPackages = async () => {
    const { data } = await api.getPackages({ package_name: 'autodroid-desktop', latest_only: true })
    return data?.items || []
  }

  /** 已暂存更新的重启确认（无需重新下载；取消则保留暂存，下次检查再问）。 */
  const promptRestart = async (staged) => {
    try {
      await ElMessageBox.confirm(
        `新版本 ${staged.version} 已下载完成，立即重启完成更新？`,
        '重启完成更新',
        { type: 'info', confirmButtonText: '立即重启', cancelButtonText: '稍后' },
      )
    } catch {
      return // 稍后
    }
    try {
      await applyUpdate({ bridge: getBridge() })
      // 壳即将替换应用包并重启进程
    } catch (err) {
      ElMessage.error(`安装更新失败：${err?.message || '未知错误'}（可重试）`)
    }
  }

  /**
   * 更新弹窗：新版本 + 更新提示（上传包时手写的 changelog / Release 正文），
   * 两个按钮「立即更新」「不再提示」。返回 'install' | 'skip' | 'dismiss'。
   * 关闭（X / Esc）不算「不再提示」：没点按钮就不该替用户做决定，下次启动照常提示。
   */
  const askUpdate = async (info) => {
    const html = buildUpdateDialogHtml({
      remoteVersion: info?.remoteVersion,
      localVersion: info?.localVersion,
      notes: info?.changelog,
    })
    try {
      await ElMessageBox.confirm(html, '发现新版本', {
        dangerouslyUseHTMLString: true,
        type: 'info',
        confirmButtonText: '立即更新',
        cancelButtonText: '不再提示',
        showClose: true,
        closeOnClickModal: false,
        closeOnPressEscape: true,
        customClass: 'autodroid-update-box',
      })
      return 'install'
    } catch (err) {
      // element-plus 取消/关闭以字符串 reject（'cancel' / 'close'）。只有明确的「不再提示」
      // 才算忽略；关闭与任何意外异常都按"没表态"处理（下次启动照常提示）。
      const reason = typeof err === 'string' ? err : String(err?.action ?? err?.message ?? '')
      return reason === 'cancel' ? 'skip' : 'dismiss'
    }
  }

  /** 「立即更新」下载完成即换包重启：用户已经点过「立即更新」，不再问第二遍。 */
  const applyStagedUpdate = async () => {
    try {
      ElMessage.success('更新已下载，正在重启完成安装')
      await applyUpdate({ bridge: getBridge() })
      // 壳即将替换应用包并重启进程
    } catch (err) {
      // 自动重启不可用（如权限/磁盘）：退回手动重启入口，用户可稍后再试
      ElMessage.error(`安装更新失败：${err?.message || '未知错误'}（可点击「检查更新」重试）`)
    }
  }

  /** 有新版时的弹窗 → 按用户选择装上，或记下该版本不再提示。 */
  const promptUpdate = async (info) => {
    const action = await askUpdate(info)
    if (action === 'install') {
      await installUpdate(info)
      return action
    }
    if (action === 'skip') {
      rememberSkippedUpdateVersion(getStorage(), info?.remoteVersion)
      ElMessage.info(`已忽略版本 ${info?.remoteVersion}，该版本不再提示`)
    }
    return action
  }

  /** 下载安装 + 进度提示（弹窗「立即更新」与静默发现共用）。 */
  const installUpdate = async (info) => {
    installing.value = true
    downloadPercent.value = 0
    let progressNotice = null
    let terminalHandled = false
    try {
      const result = await runUpdateInstall({
        pkg: info.pkg,
        release: info.release,
        token: getToken(),
        origin: getOrigin(),
        bridge: getBridge(),
        onPhase: (evt) => {
          if (evt.phase === 'downloading') {
            downloadPercent.value = evt.percent
            progressNotice = ElNotification({
              id: PROGRESS_NOTIFICATION_ID,
              title: '下载更新',
              message: `正在下载 ${info.remoteVersion}：${evt.percent}%`,
              type: 'info',
              duration: 0,
            })
            return
          }
          if (evt.phase === 'fallback') {
            // 主源（平台包管理）失败切备用源（GitHub）；进度通知原地更新，不新开提示
            progressNotice = ElNotification({
              id: PROGRESS_NOTIFICATION_ID,
              title: '下载更新',
              message: `主下载源不可用（${evt.message || '连接失败'}），正在改用备用源…`,
              type: 'warning',
              duration: 0,
            })
            return
          }
          terminalHandled = true
          progressNotice?.close?.()
          if (evt.phase === 'ready') {
            ElMessage.success('新版本已下载完成')
          } else if (evt.phase === 'downloaded') {
            ElMessage.info(evt.message || '更新包已下载并打开，请按提示完成安装')
          } else if (evt.phase === 'done') {
            ElMessage.success('安装完成后请重启应用')
          } else if (evt.phase === 'error') {
            ElMessage.error(`更新失败：${evt.message}（可重试）`)
          }
        },
      })
      if (result?.ready) {
        await applyStagedUpdate()
      }
    } catch (err) {
      // runUpdateInstall 已保证错误终态先于 reject 抛出；此处兜底防漏提示
      if (!terminalHandled) {
        progressNotice?.close?.()
        ElMessage.error(`更新失败：${err?.message || '未知错误'}（可重试）`)
      }
    } finally {
      installing.value = false
    }
  }

  /**
   * 检查流程：silent 为启动自动检查（无更新 / 不支持 / 查询失败都不打扰）；
   * 有新版一律弹更新弹窗，但自动检查会跳过用户已点过「不再提示」的版本。
   */
  const runCheck = async ({ silent }) => {
    if (checking.value || installing.value) return null
    checking.value = true
    try {
      const bridge = getBridge()
      // 已下载待重启的更新优先：离线也能直接重启完成更新（无需重新下载，也与忽略记录无关）
      const staged = await getStagedUpdate(bridge)
      if (staged) {
        let localVersion = ''
        try {
          const clientInfo = await bridge?.getClientInfo?.()
          localVersion = String(clientInfo?.version ?? '').trim()
        } catch (error) {
          localVersion = ''
        }
        if (localVersion && compareVersions(staged.version, localVersion) > 0) {
          await promptRestart(staged)
          return { supported: true, hasUpdate: true, remoteVersion: staged.version, localVersion, source: 'staged' }
        }
      }

      const info = await checkForUpdate({ listPackages: listDesktopPackages, getBridge })
      if (!info.supported) {
        if (!silent) ElMessage.info('当前环境不支持检查更新')
        return info
      }
      if (!info.hasUpdate) {
        if (!silent) {
          if (info.reason === 'no-package') ElMessage.info('暂未发现可用的更新包')
          else ElMessage.info(`已是最新版本（${info.localVersion}）`)
        }
        return info
      }
      // 自动检查只对「已点过不再提示」的版本安静；手动点检查更新是用户主动问，照常弹窗
      if (silent && isUpdateSkipped(info.remoteVersion, readSkippedUpdateVersion(getStorage()))) {
        return { ...info, skipped: true }
      }
      await promptUpdate(info)
      return info
    } catch (err) {
      if (!silent) ElMessage.error(`检查更新失败：${err?.message || '未知错误'}`)
      return null
    } finally {
      checking.value = false
    }
  }

  /** 手动「检查更新」入口（Navbar 账号下拉 / 托盘回跳同一检查流程）。 */
  const startUpdateCheck = async () => {
    await detectClientApp()
    if (!isClientApp.value) return null // 纯 Web 无入口无检查
    return runCheck({ silent: false })
  }

  /** 启动自动检查：clientMode 启动后一次，有新版直接弹更新弹窗（不阻塞启动）。 */
  const initSilentUpdateCheck = async () => {
    if (silentCheckStarted) return
    silentCheckStarted = true
    await detectClientApp()
    if (!isClientApp.value) return
    await runCheck({ silent: true })
  }

  /**
   * 托盘「检查更新」：壳 send('autodroid:check-update') → 前端触发同一检查流程。
   * 桥缺失/壳未实现时静默降级（返回空退订函数），返回值为退订函数。
   */
  const subscribeCheckUpdateRequest = () => {
    const bridge = getBridge()
    if (typeof bridge?.onCheckUpdateRequest !== 'function') return () => {}
    return bridge.onCheckUpdateRequest(() => startUpdateCheck()) || (() => {})
  }

  return {
    checking,
    installing,
    downloadPercent,
    isClientApp,
    promptUpdate,
    startUpdateCheck,
    initSilentUpdateCheck,
    subscribeCheckUpdateRequest,
  }
}
