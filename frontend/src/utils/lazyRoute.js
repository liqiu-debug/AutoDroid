/**
 * 路由懒加载韧性包装：动态 import 失败不再「点击无反应」。
 *
 * 背景（2026-10-28 实锤的「长时间不操作后点击无响应」根因）：
 * 路由页按需 import() chunk，拉取失败的两种触发：
 *   ① 闲置后休眠/网络切换 → 一次瞬时失败；
 *   ② 平台部署新前端 → 旧页面引用的 chunk 文件名已变 → 404。
 * 关键：浏览器会把「模块加载失败」缓存进模块表——同一 chunk 之后每次
 * 点击都瞬间失败，且路由守卫吞掉错误无提示，表现即「点了没反应」；
 * 刷新（新 index.html + 新模块缓存）才恢复。
 *
 * 对策：
 * 1. 瞬时失败：指数退避重试（默认 2 次）覆盖网络抖动；
 * 2. 终态失败（多为部署换版的 404）：自动 location.reload() 拉新壳——
 *    30 秒内只自动刷一次（防重载环），第二次给出可点击的错误提示。
 */

let customErrorHandler = null

/** 注入可见错误提示（如 ElMessage.error）；未注入时退化为 window.__EL_MESSAGE__。 */
export function setLazyRouteErrorHandler(handler) {
  customErrorHandler = handler
}

const RETRY_DELAYS_MS = [300, 900]
const RELOAD_GUARD_KEY = 'autodroid:chunk-reload-at'
const RELOAD_GUARD_WINDOW_MS = 30000

/** 动态 import 失败特征（网络失败/404 都会以这类信息拒绝）。 */
export function isModuleFetchFailure(error) {
  const message = String((error && error.message) || error || '')
  return /dynamically imported module|error loading dynamically imported module|importing a module script failed/i.test(message)
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function defaultReload() {
  if (typeof window !== 'undefined' && window.location) {
    window.location.reload()
  }
}

/**
 * @param {() => Promise<any>} loader 动态 import() 调用体
 * @param {object} [options] { reload, onError, now, storage } 便于测试注入
 * @returns {() => Promise<any>} 路由 component 加载器
 */
export function lazyRoute(loader, options = {}) {
  const reload = options.reload || defaultReload
  const onError = options.onError || ((message) => {
    if (typeof customErrorHandler === 'function') {
      customErrorHandler(message)
    } else if (typeof window !== 'undefined' && window.__EL_MESSAGE__) {
      window.__EL_MESSAGE__.error(message)
    }
  })
  const storage = options.storage || (typeof window !== 'undefined' ? window.sessionStorage : null)
  const now = options.now || (() => Date.now())

  return async () => {
    let lastError = null
    for (let attempt = 0; attempt <= RETRY_DELAYS_MS.length; attempt += 1) {
      try {
        const result = await loader()
        if (storage) {
          try { storage.removeItem(RELOAD_GUARD_KEY) } catch { /* 存储不可用忽略 */ }
        }
        return result
      } catch (error) {
        lastError = error
        if (!isModuleFetchFailure(error) || attempt === RETRY_DELAYS_MS.length) {
          break
        }
        await delay(RETRY_DELAYS_MS[attempt])
      }
    }

    // 终态失败：部署换版（chunk 404）居多——自动重载拉新壳一次；
    // 短窗口内已自动刷过则不再循环，改为可见提示（避免无限刷新）。
    let recentReload = false
    if (storage) {
      try {
        const stamp = Number(storage.getItem(RELOAD_GUARD_KEY) || 0)
        recentReload = stamp > 0 && now() - stamp < RELOAD_GUARD_WINDOW_MS
      } catch { /* 存储不可用忽略 */ }
    }
    if (!recentReload) {
      if (storage) {
        try { storage.setItem(RELOAD_GUARD_KEY, String(now())) } catch { /* 存储不可用忽略 */ }
      }
      reload()
      // 给重载一个挂起的 promise，避免调用方立刻抛未处理拒绝
      return new Promise(() => {})
    }

    const message = '页面资源加载失败（平台可能已更新），请点击刷新或稍后重试'
    onError(message, lastError)
    throw lastError
  }
}
