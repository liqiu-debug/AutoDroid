import { ref } from 'vue'
import { ElMessage, ElMessageBox, ElNotification } from 'element-plus'
import api from '@/api'
import { useClientApp } from '@/composables/useClientApp'
import { compareVersions } from '@/utils/semver'
import { applyUpdate, checkForUpdate, getStagedUpdate, runUpdateInstall } from '@/utils/appUpdate'

/**
 * useAppUpdate - 桌面客户端「检查更新」流程（入口 + 确认框 + 下载进度 + 内置安装重启 + 静默检查）。
 *
 * 门控：仅 clientMode（useClientApp 探测）显示入口并执行检查；纯 Web 无入口无检查。
 * 桥缺失（window.desktop 不在）降级为「当前环境不支持检查更新」，不阻塞任何既有业务。
 * 更新源：壳内 GitHub Release（优先，断点续传下载）+ 平台「App 包管理」双源取更高版本。
 * macOS 下载完成即已暂存，确认后换包重启（applyUpdate）；win32 拉起安装向导。
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
        await promptRestart({ version: info.remoteVersion })
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

  /** 检查流程：silent 静默（无更新/不支持/失败都不打扰），否则逐分支中文提示。 */
  const runCheck = async ({ silent }) => {
    if (checking.value || installing.value) return null
    checking.value = true
    try {
      const bridge = getBridge()
      // 已下载待重启的更新优先：离线也能直接重启完成更新
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
          if (silent) {
            ElNotification({
              id: DISCOVERY_NOTIFICATION_ID,
              title: '待重启完成更新',
              message: `新版本 ${staged.version} 已就绪，点击立即重启完成更新`,
              type: 'info',
              duration: 0,
              onClick: () => promptRestart(staged),
            })
          } else {
            await promptRestart(staged)
          }
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
