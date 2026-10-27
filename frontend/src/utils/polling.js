/**
 * 轮询基础设施：在途门闩 + 可见性暂停 + 轮询类 GET 短超时。
 *
 * 背景（长时间未操作后点击无响应）：无门闩的 setInterval 轮询在闲置/网络劣化时
 * 会堆积 pending XHR，打满同域 HTTP/1.1 连接池，把用户点击请求饿死在队列里。
 * 门闩语义全仓统一：
 * - 在途不重叠：上一轮请求未完成时跳过本轮（绝不并发出票）；
 * - 完成后 setTimeout 续排：节奏由上一轮完成时刻驱动，慢网络下自动降频；
 * - document.hidden / pagehide 即暂停，可见（visibilitychange / pageshow）即恢复。
 *
 * 参考实现基线：DashboardView.vue 的 `if (inflight) return` + handleVisibilityChange。
 */

/** 轮询类 GET 的显式短超时；全局 axios 默认 30s 保留，调用处可用 config 覆盖 */
export const POLL_REQUEST_TIMEOUT_MS = 8000

/**
 * 轮询类 GET 的请求配置：默认 8s 短超时，可被 overrides 覆盖（如更长的流式请求）。
 */
export const pollRequestConfig = (overrides = {}) => ({
  timeout: POLL_REQUEST_TIMEOUT_MS,
  ...overrides,
})

/**
 * 页面是否处于隐藏（后台标签 / 最小化）。无 document 环境（SSR / node 测试）恒为可见。
 */
export function isPageHidden() {
  return typeof document !== 'undefined' && Boolean(document.hidden)
}

/**
 * 在途门闩：同一任务在途时直接跳过新调用（照抄 DashboardView 的 if (inflight) return）。
 * 跳过时返回已 resolve 的 Promise<undefined>，await 调用方无需分支。
 */
export function createGatedTask(fn) {
  let inflight = false
  const gated = (...args) => {
    if (inflight) return Promise.resolve(undefined)
    inflight = true
    try {
      return Promise.resolve(fn(...args)).finally(() => {
        inflight = false
      })
    } catch (err) {
      inflight = false
      return Promise.reject(err)
    }
  }
  gated.isInflight = () => inflight
  return gated
}

/**
 * 轮询循环控制器。
 *
 * @param {object} options
 * @param {number|() => number} options.intervalMs 轮询间隔（可为函数：自适应间隔）
 * @param {() => Promise<void>|void} options.tick 每轮任务；同一时刻最多一个在途
 * @param {() => boolean} [options.isEnabled] 每轮前置条件；不满足时跳过本轮但仍续排
 * @param {(err: unknown) => void} [options.onError] tick 抛错时的回调（默认 console.error）
 * @param {boolean} [options.immediate] start 后是否立即跑一轮（默认等一个间隔）
 * @param {() => Document|null} [options.getDocument] 可注入 document（测试用）
 * @param {() => Window|null} [options.getWindow] 可注入 window（测试用）
 * @returns {{ start: Function, stop: Function, isActive: () => boolean, isInflight: () => boolean, runNow: Function }}
 */
export function createPollingLoop({
  intervalMs,
  tick,
  isEnabled = () => true,
  onError = (err) => {
    console.error('轮询任务失败:', err)
  },
  immediate = false,
  getDocument = () => (typeof document === 'undefined' ? null : document),
  getWindow = () => (typeof window === 'undefined' ? null : window),
} = {}) {
  const resolveInterval = () => {
    const value = typeof intervalMs === 'function' ? Number(intervalMs()) : Number(intervalMs)
    return Number.isFinite(value) && value >= 0 ? value : 0
  }

  let timer = null
  let desired = false
  let inflight = false
  let generation = 0
  let listenersBound = false

  const clearTimer = () => {
    if (timer != null) {
      clearTimeout(timer)
      timer = null
    }
  }

  const schedule = (delay) => {
    clearTimer()
    if (!desired) return
    const myGeneration = generation
    const wait = Number.isFinite(delay) ? delay : resolveInterval()
    timer = setTimeout(() => {
      timer = null
      void runTick(myGeneration)
    }, wait)
  }

  async function runTick(myGeneration) {
    if (!desired || myGeneration !== generation) return
    const doc = getDocument()
    if (doc && doc.hidden) return // 隐藏即暂停；可见性事件负责恢复
    if (inflight) {
      // 轮前检查：上一轮仍在途（慢网络）则跳过本轮，不并发出票
      schedule()
      return
    }
    if (!isEnabled()) {
      schedule()
      return
    }
    inflight = true
    try {
      await tick()
    } catch (err) {
      try {
        onError(err)
      } catch {
        // onError 自身的异常不拖垮轮询循环
      }
    } finally {
      inflight = false
      // 完成后 setTimeout 续排（而非 setInterval 连发）
      if (desired && myGeneration === generation) schedule()
    }
  }

  const pause = () => {
    clearTimer()
  }

  const resume = () => {
    if (!desired || timer != null || inflight) return
    schedule()
  }

  const handleVisibilityChange = () => {
    const doc = getDocument()
    if (!desired) return
    if (doc && doc.hidden) pause()
    else resume()
  }

  const handlePageHide = () => {
    // pagehide（导航 / 刷新 / 进 bfcache）时停止续排，释放连接池
    pause()
  }

  const handlePageShow = () => {
    resume()
  }

  const bindListeners = () => {
    if (listenersBound) return
    listenersBound = true
    const doc = getDocument()
    const win = getWindow()
    if (doc && typeof doc.addEventListener === 'function') {
      doc.addEventListener('visibilitychange', handleVisibilityChange)
    }
    if (win && typeof win.addEventListener === 'function') {
      win.addEventListener('pagehide', handlePageHide)
      win.addEventListener('pageshow', handlePageShow)
    }
  }

  const unbindListeners = () => {
    if (!listenersBound) return
    listenersBound = false
    const doc = getDocument()
    const win = getWindow()
    if (doc && typeof doc.removeEventListener === 'function') {
      doc.removeEventListener('visibilitychange', handleVisibilityChange)
    }
    if (win && typeof win.removeEventListener === 'function') {
      win.removeEventListener('pagehide', handlePageHide)
      win.removeEventListener('pageshow', handlePageShow)
    }
  }

  return {
    start() {
      if (desired) return
      desired = true
      generation++
      bindListeners()
      const doc = getDocument()
      if (doc && doc.hidden) return // 隐藏中不排轮，可见后由 visibilitychange 恢复
      schedule(immediate ? 0 : undefined)
    },
    stop() {
      desired = false
      generation++
      clearTimer()
      unbindListeners()
    },
    isActive: () => desired,
    isInflight: () => inflight,
    /** 立即补一轮（仍受在途门闩 / 可见性约束） */
    runNow() {
      if (!desired) return
      schedule(0)
    },
  }
}
