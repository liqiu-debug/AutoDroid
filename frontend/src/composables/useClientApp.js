import { ref } from 'vue'
import api from '@/api'

/**
 * useClientApp - 客户端（桌面引擎）模式探测，进程内缓存一次结果。
 *
 * GET /api/client/info 仅存在于桌面引擎（200 + {clientMode:true}）；
 * 纯 Web 对云端请求得 404/传输失败 → 判为 Web 模式，UI 行为零变化。
 * 与 LoginView/Register 的登录期探测同源语义，此处供登录后页面复用
 * （如：iOS IPA 一键安装在桌面引擎恒 501，需在客户端模式隐藏/降级）。
 */
const isClientApp = ref(false)
const probeDone = ref(false)
let probePromise = null

export function useClientApp() {
  /** 幂等：首次调用发起探测，其后返回同一 Promise（避免打开弹窗时与探测竞态）。 */
  const detectClientApp = () => {
    if (probePromise) return probePromise
    probePromise = (async () => {
      try {
        const res = await api.getClientInfo()
        isClientApp.value = res?.data?.clientMode === true
      } catch (err) {
        // 404 / 传输失败 = 纯 Web 模式，维持默认
        isClientApp.value = false
      } finally {
        probeDone.value = true
      }
      return isClientApp.value
    })()
    return probePromise
  }

  return { isClientApp, probeDone, detectClientApp }
}
