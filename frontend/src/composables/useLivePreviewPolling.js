import { computed } from 'vue'

const IOS_LIVE_PREVIEW_POLL_MS = 900
const ANDROID_LIVE_PREVIEW_POLL_IDLE_MS = 700
const ANDROID_LIVE_PREVIEW_POLL_ACTIVE_MS = 300
const LIVE_PREVIEW_BUSY_POLL_MS = 1800
const LIVE_PREVIEW_POLL_ACTIVE_WINDOW_MS = 2500

/**
 * 实时预览层级轮询：
 * - 根据平台 / 设备忙闲 / 交互活跃度自适应轮询间隔
 * - 框选、加载中等状态下暂停轮询
 * - 交互后的加速窗口（boost）提升层级同步实时性
 */
export function useLivePreviewPolling({
  isStageActive,
  liveMode,
  selectedSerial,
  isIosLivePreview,
  isSelectedStreamReady,
  isSelectedDeviceBusy,
  loading,
  liveHierarchyLoading,
  ocrCropMode,
  imageCropMode,
  quickImagePrompt,
  fetchLiveHierarchy
}) {
  let livePreviewPollTimer = null
  let livePreviewBoostUntil = 0

  const shouldPauseLivePreviewPolling = computed(() => {
    return Boolean(
      loading.value
      || liveHierarchyLoading.value
      || ocrCropMode.value
      || imageCropMode.value
      || quickImagePrompt.value
    )
  })

  const shouldPollLivePreview = computed(() => {
    if (!liveMode.value || !selectedSerial.value) return false
    if (isIosLivePreview.value) return true
    return isSelectedStreamReady.value
  })

  const getLivePreviewPollInterval = () => {
    if (isSelectedDeviceBusy.value) return LIVE_PREVIEW_BUSY_POLL_MS
    if (isIosLivePreview.value) return IOS_LIVE_PREVIEW_POLL_MS
    return Date.now() < livePreviewBoostUntil
      ? ANDROID_LIVE_PREVIEW_POLL_ACTIVE_MS
      : ANDROID_LIVE_PREVIEW_POLL_IDLE_MS
  }

  const clearLivePreviewPollTimer = () => {
    if (!livePreviewPollTimer) return
    clearTimeout(livePreviewPollTimer)
    livePreviewPollTimer = null
  }

  const startLivePreviewPolling = ({ immediate = false } = {}) => {
    if (!isStageActive.value || !shouldPollLivePreview.value || livePreviewPollTimer) return
    const delay = immediate ? 0 : getLivePreviewPollInterval()
    livePreviewPollTimer = setTimeout(async () => {
      livePreviewPollTimer = null
      if (!isStageActive.value || !shouldPollLivePreview.value) return
      if (!shouldPauseLivePreviewPolling.value) {
        await fetchLiveHierarchy()
      }
      startLivePreviewPolling()
    }, delay)
  }

  const stopLivePreviewPolling = () => {
    clearLivePreviewPollTimer()
  }

  const syncLivePreviewPolling = ({ immediate = false } = {}) => {
    stopLivePreviewPolling()
    if (shouldPollLivePreview.value) {
      startLivePreviewPolling({ immediate })
    }
  }

  const bumpLivePreviewPollingBoost = (durationMs = LIVE_PREVIEW_POLL_ACTIVE_WINDOW_MS) => {
    livePreviewBoostUntil = Math.max(livePreviewBoostUntil, Date.now() + durationMs)
    syncLivePreviewPolling({ immediate: true })
  }

  const resetLivePreviewBoost = () => {
    livePreviewBoostUntil = 0
  }

  return {
    shouldPollLivePreview,
    startLivePreviewPolling,
    stopLivePreviewPolling,
    syncLivePreviewPolling,
    bumpLivePreviewPollingBoost,
    resetLivePreviewBoost
  }
}
