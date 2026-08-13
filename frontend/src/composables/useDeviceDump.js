import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import { parseHierarchy } from '@/utils/hierarchy'

// iOS MJPEG 实时流健康时，静态截图退化为低频兜底刷新（层级轮询节奏不变）
const IOS_STREAM_HEALTHY_SCREENSHOT_INTERVAL_MS = 3000

/**
 * 设备截图 / UI 层级刷新：
 * - 持有截图、层级 XML、解析后的节点列表与设备信息
 * - 静态截图刷新（fetchDump）与投屏模式层级刷新（fetchLiveHierarchy）
 * - iOS 实时预览的轻量 dump 节流（隔次跳过层级抓取）
 * - iOS MJPEG 流健康时（isIosStreamHealthy，可选）降低静态截图刷新频率
 */
export function useDeviceDump({ stage, selectedSerial, isIosLivePreview, isIosStreamHealthy, onBeforeFetchDump }) {
  const { isStageActive, isAbortError, createDumpRequestSignal } = stage

  const screenshot = ref('')
  const hierarchyXml = ref('')
  const hierarchyHash = ref('')
  const deviceInfo = ref(null)
  const nodes = ref([])
  const liveNodes = ref([])  // 投屏模式下的 UI 层级节点
  const loading = ref(false)
  const liveHierarchyLoading = ref(false)
  const liveHierarchyStatus = ref('idle')
  const liveHierarchyError = ref('')
  const liveHierarchyLastUpdatedAt = ref(0)

  let iosLivePreviewLightDumpCount = 0
  let lastScreenshotUpdatedAt = 0

  const resetIosLightDumpCount = () => {
    iosLivePreviewLightDumpCount = 0
  }

  const markLiveHierarchyFresh = () => {
    liveHierarchyStatus.value = 'fresh'
    liveHierarchyError.value = ''
    liveHierarchyLastUpdatedAt.value = Date.now()
  }

  const markLiveHierarchyStale = (errorMessage = '') => {
    liveHierarchyStatus.value = 'stale'
    liveHierarchyError.value = String(errorMessage || '').trim()
  }

  const resetLiveHierarchyMeta = () => {
    liveHierarchyStatus.value = 'idle'
    liveHierarchyError.value = ''
    liveHierarchyLastUpdatedAt.value = 0
  }

  const shouldRequestDeviceInfo = () => {
    return !deviceInfo.value || String(deviceInfo.value?.serial || '') !== String(selectedSerial.value || '')
  }

  const getLiveDumpOptions = ({ forceHierarchy = false } = {}) => {
    if (!isIosLivePreview.value) {
      return {
        includeScreenshot: false,
        includeHierarchy: true,
        includeDeviceInfo: shouldRequestDeviceInfo()
      }
    }

    const includeHierarchy = Boolean(
      forceHierarchy
      || !hierarchyXml.value
      || !liveNodes.value.length
      || iosLivePreviewLightDumpCount >= 2
    )

    if (includeHierarchy) {
      iosLivePreviewLightDumpCount = 0
    } else {
      iosLivePreviewLightDumpCount += 1
    }

    // MJPEG 实时流健康时，静态截图仅作低频兜底（画面由实时层承担）；
    // 流断开或截图超期（3s）时恢复每次携带，保证框选/图像截取的数据源可用。
    const streamHealthy = Boolean(isIosStreamHealthy?.value)
    const screenshotFresh = Boolean(
      streamHealthy
      && lastScreenshotUpdatedAt > 0
      && (Date.now() - lastScreenshotUpdatedAt) < IOS_STREAM_HEALTHY_SCREENSHOT_INTERVAL_MS
    )

    return {
      includeScreenshot: !screenshotFresh,
      includeHierarchy,
      includeDeviceInfo: shouldRequestDeviceInfo()
    }
  }

  const updateStateFromDump = (dump) => {
    if (!dump) return
    if (dump.screenshot) {
      const format = dump.screenshot_format || 'png'
      screenshot.value = `data:image/${format};base64,${dump.screenshot}`
      lastScreenshotUpdatedAt = Date.now()
    }
    if (dump.device_info) {
      deviceInfo.value = dump.device_info
    }
    if (dump.hierarchy_xml) {
      const nextHierarchyXml = String(dump.hierarchy_xml || '')
      const nextHierarchyHash = String(dump.hierarchy_hash || '').trim()
      const shouldReparse = Boolean(
        !nextHierarchyHash
        || nextHierarchyHash !== hierarchyHash.value
        || nextHierarchyXml !== hierarchyXml.value
      )

      hierarchyXml.value = nextHierarchyXml
      hierarchyHash.value = nextHierarchyHash

      if (shouldReparse) {
        const parsedNodes = parseHierarchy(nextHierarchyXml, dump.device_info || deviceInfo.value)
        nodes.value = parsedNodes
        liveNodes.value = parsedNodes
      }
    }
  }

  // Fetch device dump
  const fetchDump = async () => {
    if (onBeforeFetchDump) {
      onBeforeFetchDump()
    }
    if (!isStageActive.value || !selectedSerial.value) {
      screenshot.value = ''
      hierarchyXml.value = ''
      hierarchyHash.value = ''
      deviceInfo.value = null
      nodes.value = []
      liveNodes.value = []
      lastScreenshotUpdatedAt = 0
      resetLiveHierarchyMeta()
      return
    }
    loading.value = true
    const { signal, release } = createDumpRequestSignal()
    try {
      const res = await api.getDeviceDump(selectedSerial.value, {
        includeScreenshot: true,
        includeHierarchy: true,
        includeDeviceInfo: shouldRequestDeviceInfo(),
        signal
      })
      if (!isStageActive.value) return
      updateStateFromDump(res.data)
    } catch (err) {
      if (!isAbortError(err)) {
        ElMessage.error('获取设备状态失败: ' + (err.response?.data?.detail || err.message))
      }
    } finally {
      release()
      if (isStageActive.value) {
        loading.value = false
      }
    }
  }

  // 投屏模式下获取 UI 层级（用于元素高亮）
  const fetchLiveHierarchy = async ({ showLoading = false, forceHierarchy = false } = {}) => {
    if (!isStageActive.value || !selectedSerial.value) {
      liveHierarchyStatus.value = 'idle'
      liveHierarchyError.value = ''
      return
    }
    if (liveHierarchyLoading.value) return
    const dumpOptions = getLiveDumpOptions({ forceHierarchy })
    if (!dumpOptions.includeScreenshot && !dumpOptions.includeHierarchy && !dumpOptions.includeDeviceInfo) {
      // 本轮无需抓取任何数据（实时流健康 + 截图新鲜的轻量档），直接跳过请求
      return
    }
    const hierarchyRequested = Boolean(dumpOptions.includeHierarchy)
    if (hierarchyRequested) {
      liveHierarchyStatus.value = 'syncing'
    }
    liveHierarchyLoading.value = true
    if (showLoading) {
      loading.value = true
    }
    const { signal, release } = createDumpRequestSignal()
    try {
      const res = await api.getDeviceDump(selectedSerial.value, {
        ...dumpOptions,
        signal
      })
      if (!isStageActive.value) return
      updateStateFromDump(res.data)
      if (hierarchyRequested) {
        if (res?.data?.hierarchy_xml) {
          markLiveHierarchyFresh()
        } else {
          markLiveHierarchyStale('层级结果为空')
        }
      }
    } catch (err) {
      if (!isAbortError(err)) {
        console.warn('获取投屏层级失败:', err.response?.data?.detail || err.message)
        markLiveHierarchyStale(err.response?.data?.detail || err.message)
      }
    } finally {
      release()
      if (isStageActive.value) {
        liveHierarchyLoading.value = false
        if (showLoading) {
          loading.value = false
        }
      }
    }
  }

  return {
    screenshot,
    hierarchyXml,
    hierarchyHash,
    deviceInfo,
    nodes,
    liveNodes,
    loading,
    liveHierarchyLoading,
    liveHierarchyStatus,
    liveHierarchyError,
    liveHierarchyLastUpdatedAt,
    resetIosLightDumpCount,
    markLiveHierarchyFresh,
    markLiveHierarchyStale,
    resetLiveHierarchyMeta,
    shouldRequestDeviceInfo,
    updateStateFromDump,
    fetchDump,
    fetchLiveHierarchy
  }
}
