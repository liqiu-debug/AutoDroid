/**
 * sessionExpiry - 登录令牌存在性查询与 401 会话过期的单飞处理收口。
 *
 * 闲置网络劣化恢复后，在途请求会成批打回 401：没有单飞门闩时每个 401 都会各弹
 * 一框、各跳一次转；登出后轮询仍持续打网络会继续堆积连接池。本模块收口两类行为：
 *
 * - hasSessionToken(storage)：轮询/后台请求的「发起前」门闩——token 已清就不再发
 *   请求（401 处理/手动登出后所有轮询自动静默停止）；
 * - createSessionExpiredController()：模块级 redirecting 单飞标志，只处理首个 401；
 *   未登录（localStorage 无 token）时静默放行，绝不弹框；「会话过期」处理器可注入
 *   （默认 ElMessageBox.confirm「登录状态已过期，请重新登录」→ 用户点击后
 *   router.push('/login')——用户手势内跳转，绕开 Electron 壳吞程序化跳转/刷新）；
 *   状态清理统一走 useUserStore.logout()（pinia token 与 localStorage 一并清）。
 *
 * 纯逻辑 + 全依赖注入，node:test 可直接单测（与 utils/streamAuth.js 同风格）；
 * 默认弹框/跳转/清理实现只在调用时惰性 import，避免与 router/store 形成模块环。
 */

export const TOKEN_STORAGE_KEY = 'token'
export const SESSION_EXPIRED_MESSAGE = '登录状态已过期，请重新登录'
export const SESSION_EXPIRED_TITLE = '会话已过期'
export const SESSION_EXPIRED_CONFIRM_TEXT = '重新登录'
export const SESSION_EXPIRED_CANCEL_TEXT = '取消'

const resolveStorage = (storage) => {
  if (storage) return storage
  if (typeof window !== 'undefined' && window.localStorage) return window.localStorage
  return null
}

/** 当前是否存在登录令牌（localStorage）。轮询/后台请求发起前的静默门闩。 */
export function hasSessionToken(storage) {
  const store = resolveStorage(storage)
  try {
    return !!store?.getItem(TOKEN_STORAGE_KEY)
  } catch {
    return false
  }
}

/** 默认状态清理：统一经 useUserStore.logout() 清 pinia token 与 localStorage。 */
export const defaultSessionLogout = async () => {
  try {
    const { useUserStore } = await import('@/stores/useUserStore')
    useUserStore().logout()
  } catch {
    try {
      resolveStorage()?.removeItem(TOKEN_STORAGE_KEY)
    } catch {
      /* 无可清时忽略 */
    }
  }
}

/** 默认会话过期弹框（ElMessageBox.confirm）：确认 resolve、取消 reject。 */
export const defaultSessionExpiredConfirm = (message, title, options) =>
  import('element-plus').then(m => m.ElMessageBox.confirm(message, title, options))

/** 默认跳转登录页：router.push（SPA 内导航，web/桌面同一行为）。 */
export const defaultSessionExpiredNavigate = (path) =>
  import('@/router').then(m => m.default.push(path))

/**
 * 会话过期单飞控制器。
 * options.storage   令牌存储（默认 window.localStorage，测试注入 fake）
 * options.logout    状态清理（默认 defaultSessionLogout → useUserStore.logout()）
 * options.handler   可注入「会话过期」处理器；缺省走 confirm → navigate('/login')
 * options.confirm   弹框实现（默认 defaultSessionExpiredConfirm）
 * options.navigate  跳转实现（默认 defaultSessionExpiredNavigate）
 * handleSessionExpired() 永不 reject；返回的 Promise 便于测试等待呈现完成。
 */
export function createSessionExpiredController(options = {}) {
  const storage = options.storage
  const logout = typeof options.logout === 'function' ? options.logout : defaultSessionLogout
  const confirmDialog = typeof options.confirm === 'function' ? options.confirm : defaultSessionExpiredConfirm
  const navigate = typeof options.navigate === 'function' ? options.navigate : defaultSessionExpiredNavigate
  const message = options.message || SESSION_EXPIRED_MESSAGE
  const title = options.title || SESSION_EXPIRED_TITLE

  let redirecting = false
  let handler = typeof options.handler === 'function' ? options.handler : null

  const presentDefault = async () => {
    try {
      await confirmDialog(message, title, {
        confirmButtonText: SESSION_EXPIRED_CONFIRM_TEXT,
        cancelButtonText: SESSION_EXPIRED_CANCEL_TEXT,
        type: 'warning',
      })
    } catch {
      return // 用户取消：留在当前页，路由守卫会在下次导航时收口
    }
    await navigate('/login')
  }

  const present = () => (handler ? Promise.resolve(handler({ navigate, message })) : presentDefault())

  const handleSessionExpired = () => {
    // 未登录（token 已清）：401 只剩噪声，静默放行，绝不弹框
    if (!hasSessionToken(storage)) return Promise.resolve()
    // 单飞：只处理首个 401，呈现未结束前的其余 401 一律静默（只弹一框）
    if (redirecting) return Promise.resolve()
    redirecting = true
    return (async () => {
      try {
        try {
          await logout()
        } catch {
          /* 清理失败不阻断提示 */
        }
        await present()
      } catch {
        /* 提示失败不再向上抛 */
      } finally {
        redirecting = false
      }
    })()
  }

  return {
    handleSessionExpired,
    /** 注入/替换「会话过期」处理器；传非函数恢复默认弹框流程。 */
    setHandler(next) {
      handler = typeof next === 'function' ? next : null
    },
    isRedirecting: () => redirecting,
  }
}
