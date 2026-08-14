import { computed } from 'vue'

export const LIVE_PREVIEW_POLL_CONFIG = Object.freeze({
  iosMs: 1500,
  androidIdleMs: 2500,
  androidActiveMs: 500,
  busyMs: 2500,
  activeWindowMs: 1500
})

const IOS_LIVE_PREVIEW_POLL_MS = LIVE_PREVIEW_POLL_CONFIG.iosMs
const ANDROID_LIVE_PREVIEW_POLL_IDLE_MS = LIVE_PREVIEW_POLL_CONFIG.androidIdleMs
const ANDROID_LIVE_PREVIEW_POLL_ACTIVE_MS = LIVE_PREVIEW_POLL_CONFIG.androidActiveMs
const LIVE_PREVIEW_BUSY_POLL_MS = LIVE_PREVIEW_POLL_CONFIG.busyMs
const LIVE_PREVIEW_POLL_ACTIVE_WINDOW_MS = LIVE_PREVIEW_POLL_CONFIG.activeWindowMs

/**
 * 实时预览层级轮询：
 * - 根据平台 / 设备忙闲 / 交互活跃度自适应轮询间隔
 * - 框选、加载中等状态下暂停轮询
 * - 交互后的短暂加速窗口（boost）提升层级同步实时性；画面实时性由投屏承担，
 *   层级不再持续高频拉取，避免与视频和执行命令争用无线 ADB。
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
