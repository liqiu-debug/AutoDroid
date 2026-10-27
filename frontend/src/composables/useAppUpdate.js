import { ref } from 'vue'
import { ElMessage, ElMessageBox, ElNotification } from 'element-plus'
import api from '@/api'
import { useClientApp } from '@/composables/useClientApp'
import { checkForUpdate, runUpdateInstall } from '@/utils/appUpdate'

/**
 * useAppUpdate - 桌面客户端「检查更新」流程（入口 + 确认框 + 下载进度 + 静默检查）。
 *
 * 门控：仅 clientMode（useClientApp 探测）显示入口并执行检查；纯 Web 无入口无检查。
 * 桥缺失（window.desktop 不在）降级为「当前环境不支持检查更新」，不阻塞任何既有业务。
 * 更新源 = 平台「App 包管理」：GET /api/packages/?package_name=autodroid-desktop&latest_only=true，
 * 按 window.desktop.getClientInfo().platform（darwin→macos、win32→windows）选条目，
 * compareVersions 仅当远端 > 本地才提示；下载 URL 现拼 token 交壳安装。
 */

// 下载进度 / 静默发现共用固定 id：同 id 通知原地替换，避免进度事件刷屏
const PROGRESS_NOTIFICATION_ID = 'autodroid-update-progress'
const DISCOVERY_NOTIFICATION_ID = 'autodroid-update-discovery'

// 启动静默检查每会话至多一次（间隔 ≥ 该会话）
let silentCheckStarted = false

export function useAppUpdate() {
  const { isClientApp, detectClientApp } = useClientApp()
  const checking = ref(false)
  const installing = ref(false)
  const downloadPercent = ref(0)

  const getBridge = () => (typeof window === 'undefined' ? null : (window.desktop || null))
  const getToken = () => (typeof localStorage === 'undefined' ? '' : (localStorage.getItem('token') || ''))
  const getOrigin = () => (typeof location === 'undefined' ? '' : (location.origin || ''))

  /** 更新源查询：客户端检查更新自查询形态（平台注释写明）。 */
  const listDesktopPackages = async () => {
    const { data } = await api.getPackages({ package_name: 'autodroid-desktop', latest_only: true })
    return data?.items || []
  }

  /** 有新版时的确认框 → 下载安装 + 进度提示（静默通知点击与手动检查共用）。 */
  const promptInstall = async (info) => {
    try {
      await ElMessageBox.confirm(
        `发现新版本 ${info.remoteVersion}（当前 ${info.localVersion}）`,
        '检查更新',
        { type: 'info', confirmButtonText: '下载安装', cancelButtonText: '暂不更新' },
      )
    } catch {
      return // 暂不更新
    }

    installing.value = true
    downloadPercent.value = 0
    let progressNotice = null
    let terminalHandled = false
    try {
      await runUpdateInstall({
        pkg: info.pkg,
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
          terminalHandled = true
          progressNotice?.close?.()
          if (evt.phase === 'done') {
            ElMessage.success('安装完成后请重启应用')
          } else if (evt.phase === 'error') {
            ElMessage.error(`更新失败：${evt.message}（可重试）`)
          }
        },
      })
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

  /** 检查流程：silent 静默（无更新/不支持/失败都不打扰），否则逐分支中文提示。 */
  const runCheck = async ({ silent }) => {
    if (checking.value || installing.value) return null
    checking.value = true
    try {
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
      if (silent) {
        ElNotification({
          id: DISCOVERY_NOTIFICATION_ID,
          title: '发现新版本',
          message: `发现新版本 ${info.remoteVersion}（当前 ${info.localVersion}），点击下载安装`,
          type: 'info',
          duration: 0,
          onClick: () => promptInstall(info),
        })
        return info
      }
      await promptInstall(info)
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

  /** 启动静默检查：clientMode 启动后一次，有新版弹通知（点击开确认框），不阻塞。 */
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
    promptInstall,
    startUpdateCheck,
    initSilentUpdateCheck,
    subscribeCheckUpdateRequest,
  }
}
