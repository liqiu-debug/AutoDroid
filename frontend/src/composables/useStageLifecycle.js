import { ref } from 'vue'

/**
 * 设备舞台生命周期守卫：
 * - isStageActive 标记组件是否处于激活状态（挂载 / keep-alive 激活）
 * - 管理 dump 请求的 AbortController 池，失活时批量取消在途请求
 */
export function useStageLifecycle() {
  const isStageActive = ref(true)
  const activeDumpControllers = new Set()

  const isAbortError = (err) => {
    return err?.name === 'CanceledError'
      || err?.code === 'ERR_CANCELED'
      || err?.name === 'AbortError'
  }

  const createDumpRequestSignal = () => {
    if (!isStageActive.value || typeof AbortController === 'undefined') {
      return { signal: undefined, release: () => {} }
    }

    const controller = new AbortController()
    activeDumpControllers.add(controller)
    return {
      signal: controller.signal,
      release: () => {
        activeDumpControllers.delete(controller)
      }
    }
  }

  const cancelActiveDumpRequests = () => {
    activeDumpControllers.forEach((controller) => controller.abort())
    activeDumpControllers.clear()
  }

  return {
    isStageActive,
    isAbortError,
    createDumpRequestSignal,
    cancelActiveDumpRequests
  }
}
