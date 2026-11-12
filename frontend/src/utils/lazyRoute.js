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
// 自动重载后若页面迟迟没被换掉（webview 拦截 reload 等），超时转为可见提示并拒绝，
// 避免路由 component 永远不解析——那正是本模块要消灭的「点击无响应」。
const RELOAD_SETTLE_TIMEOUT_MS = 4000

const RESOURCE_LOAD_FAILED_MESSAGE = '页面资源加载失败（平台可能已更新），请点击刷新或稍后重试'
const COMPONENT_LOAD_FAILED_MESSAGE = '页面组件加载出错，请刷新重试或联系管理员'

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
  // 重试退避可注入（测试里传 [0, 0] 免得等真实延迟）
  const retryDelays = Array.isArray(options.retryDelaysMs) ? options.retryDelaysMs : RETRY_DELAYS_MS

  return async () => {
    let lastError = null
    for (let attempt = 0; attempt <= retryDelays.length; attempt += 1) {
      try {
        const result = await loader()
        if (storage) {
          try { storage.removeItem(RELOAD_GUARD_KEY) } catch { /* 存储不可用忽略 */ }
        }
        return result
      } catch (error) {
        lastError = error
        if (!isModuleFetchFailure(error) || attempt === retryDelays.length) {
          break
        }
        await delay(retryDelays[attempt])
      }
    }

    // 只有「模块拉取失败」才当作部署换版（chunk 404）自动重载拉新壳一次；
    // 其它错误（组件代码 bug 等）不重载——刷新只会复现并掩盖堆栈，直接抛给路由守卫。
    // 短窗口内已自动刷过则不再循环，改为可见提示（避免无限刷新）。
    const moduleFailure = isModuleFetchFailure(lastError)
    let recentReload = false
    if (moduleFailure && storage) {
      try {
        const stamp = Number(storage.getItem(RELOAD_GUARD_KEY) || 0)
        recentReload = stamp > 0 && now() - stamp < RELOAD_GUARD_WINDOW_MS
      } catch { /* 存储不可用忽略 */ }
    }
    if (moduleFailure && !recentReload) {
      if (storage) {
        try { storage.setItem(RELOAD_GUARD_KEY, String(now())) } catch { /* 存储不可用忽略 */ }
      }
      reload()
      // 重载生效则此 promise 随页面销毁；未生效时超时兜底，绝不永久挂起。
      // 注意：不要 unref 这个定时器——unref 后事件循环可能先空转退出，
      // 兜底就永远不触发（CI 上表现为 "Promise resolution is still pending"）。
      return new Promise((_resolve, reject) => {
        setTimeout(() => {
          onError(RESOURCE_LOAD_FAILED_MESSAGE, lastError)
          reject(lastError)
        }, options.reloadSettleMs || RELOAD_SETTLE_TIMEOUT_MS)
      })
    }

    onError(moduleFailure ? RESOURCE_LOAD_FAILED_MESSAGE : COMPONENT_LOAD_FAILED_MESSAGE, lastError)
    throw lastError
  }
}
