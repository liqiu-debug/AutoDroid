import { computed, ref } from 'vue'
import api from '@/api'
import { getErrorDetail } from '@/utils/errors'
import { parseResolution } from '@/utils/hierarchy'
import { createGatedTask, createPollingLoop, pollRequestConfig } from '@/utils/polling'

const LIVE_STREAM_RECONNECT_RETRY_MS = 8000
const DEVICE_STREAM_POLL_INTERVAL_MS = 2000

/**
 * 设备选择与投屏流通道管理：
 * - 已连接设备 / 投屏流设备列表与选中态派生
 * - 设备列表轮询（投屏模式下每 2s 刷新）
 * - 投屏通道（scrcpy WebSocket 流）自动重建与手动重试
 */
export function useDeviceStreams({ liveMode, isStageActive }) {
  const connectedDevices = ref([])
  const streamDevices = ref([])
  const selectedSerial = ref('')

  const recordingDevices = computed(() =>
    connectedDevices.value.filter((d) => ['android', 'ios'].includes(String(d.platform || 'android').toLowerCase()))
  )
  const selectedRecordingDevice = computed(() =>
    connectedDevices.value.find(d => d.serial === selectedSerial.value) || null
  )
  const selectedRecordingPlatform = computed(() =>
    String(selectedRecordingDevice.value?.platform || 'android').toLowerCase()
  )
  const isSelectedDeviceBusy = computed(() =>
    String(selectedRecordingDevice.value?.status || '').toUpperCase() === 'BUSY'
  )
  const isIosLivePreview = computed(() =>
    Boolean(liveMode.value && selectedSerial.value && selectedRecordingPlatform.value === 'ios')
  )
  const selectedStreamDevice = computed(() =>
    streamDevices.value.find(d => d.serial === selectedSerial.value) || null
  )
  const isSelectedStreamReady = computed(() => Boolean(selectedStreamDevice.value?.ready))
  const liveStreamReconnectInFlight = ref(false)
  const liveStreamReconnectError = ref('')
  const selectedStreamError = computed(() => String(selectedStreamDevice.value?.error || liveStreamReconnectError.value || '').trim())
  const selectedStreamPendingText = computed(() => {
    if (liveStreamReconnectInFlight.value) return '正在重建投屏通道，请稍候...'
    return selectedStreamError.value || '投屏通道初始化中，请稍候...'
  })

  // 获取选中设备信息（屏幕尺寸）
  const selectedDevice = computed(() => {
    return connectedDevices.value.find(d => d.serial === selectedSerial.value)
  })

  const selectedDeviceScreenSize = computed(() => {
    const size = parseResolution(selectedDevice.value?.resolution)
    if (size.width > 0 && size.height > 0) return size
    return {
      width: Number(selectedStreamDevice.value?.screen_width) || 0,
      height: Number(selectedStreamDevice.value?.screen_height) || 0
    }
  })

  const canObserveDeviceInCurrentMode = (device) => {
    const status = String(device?.status || '').trim().toUpperCase()
    if (!status) return false
    if (liveMode.value) {
      return status !== 'OFFLINE' && status !== 'WDA_DOWN'
    }
    return status === 'IDLE'
  }

  let lastLiveStreamReconnectAt = 0
  let lastLiveStreamReconnectSerial = ''

  // 获取设备列表（在途门闩：慢网络下不叠加新请求，见 utils/polling.js）
  const fetchDevices = createGatedTask(async ({ ensureStream = true } = {}) => {
    if (!isStageActive.value) return
    try {
      const [deviceRes, streamRes] = await Promise.all([
        api.getDeviceList(pollRequestConfig()).catch(() => ({ data: [] })),
        api.getDevices(pollRequestConfig()).catch(() => ({ data: [] }))
      ])

      if (!isStageActive.value) return

      connectedDevices.value = Array.isArray(deviceRes.data) ? deviceRes.data : []
      streamDevices.value = Array.isArray(streamRes.data) ? streamRes.data : []

      const idleDevice = recordingDevices.value.find(d => d.status === 'IDLE')
      const fallbackDevice = liveMode.value
        ? (idleDevice || recordingDevices.value.find(d => canObserveDeviceInCurrentMode(d)))
        : idleDevice
      const selectedValid = recordingDevices.value.some(
        d => d.serial === selectedSerial.value
      )

      if (!selectedValid) {
        selectedSerial.value = fallbackDevice ? fallbackDevice.serial : ''
      }
      if (selectedStreamDevice.value) {
        liveStreamReconnectError.value = ''
      }
      if (ensureStream) {
        ensureLiveStreamChannel({ refreshAfter: false })
      }
    } catch (err) {
      console.error('获取设备列表失败:', err)
    }
  })

  // 投屏模式下 2s 轮询设备列表：在途不重叠 + 页面隐藏即暂停（utils/polling.js 统一门闩）
  const liveStreamPollLoop = createPollingLoop({
    intervalMs: DEVICE_STREAM_POLL_INTERVAL_MS,
    isEnabled: () => isStageActive.value,
    tick: () => fetchDevices()
  })

  const startLiveStreamPolling = () => {
    if (!isStageActive.value) return
    liveStreamPollLoop.start()
  }

  const stopLiveStreamPolling = () => {
    liveStreamPollLoop.stop()
  }

  const shouldEnsureLiveStreamChannel = ({ force = false } = {}) => {
    if (!isStageActive.value || !liveMode.value || !selectedSerial.value || isIosLivePreview.value) return false
    if (!force && isSelectedStreamReady.value) return false
    const streamDevice = selectedStreamDevice.value
    if (streamDevice?.initializing) return false
    return Boolean(force || !streamDevice || streamDevice.error)
  }

  const ensureLiveStreamChannel = async ({ force = false, refreshAfter = true } = {}) => {
    if (!shouldEnsureLiveStreamChannel({ force }) || liveStreamReconnectInFlight.value) return false
    const serial = selectedSerial.value
    const now = Date.now()
    const recentlyRetried = lastLiveStreamReconnectSerial === serial
      && now - lastLiveStreamReconnectAt < LIVE_STREAM_RECONNECT_RETRY_MS
    if (!force && recentlyRetried) return false

    lastLiveStreamReconnectSerial = serial
    lastLiveStreamReconnectAt = now
    liveStreamReconnectInFlight.value = true
    liveStreamReconnectError.value = ''
    try {
      await api.reconnectDeviceStream(serial)
      if (refreshAfter) {
        await fetchDevices({ ensureStream: false })
      }
      return true
    } catch (err) {
      liveStreamReconnectError.value = getErrorDetail(err, '投屏通道重建失败')
      return false
    } finally {
      liveStreamReconnectInFlight.value = false
    }
  }

  const retryLiveStreamChannel = async () => {
    await ensureLiveStreamChannel({ force: true, refreshAfter: true })
  }

  return {
    connectedDevices,
    streamDevices,
    selectedSerial,
    recordingDevices,
    selectedRecordingDevice,
    selectedRecordingPlatform,
    isSelectedDeviceBusy,
    isIosLivePreview,
    selectedStreamDevice,
    isSelectedStreamReady,
    liveStreamReconnectInFlight,
    liveStreamReconnectError,
    selectedStreamError,
    selectedStreamPendingText,
    selectedDevice,
    selectedDeviceScreenSize,
    canObserveDeviceInCurrentMode,
    fetchDevices,
    startLiveStreamPolling,
    stopLiveStreamPolling,
    ensureLiveStreamChannel,
    retryLiveStreamChannel
  }
}
