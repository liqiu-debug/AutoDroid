<script setup>
/**
 * IosMjpegPlayer - iOS 实时画面播放器（WDA MJPEG over WebSocket）
 *
 * 连接 `/ws/ios-mjpeg/{serial}`（挂载方式同 /ws/scrcpy），每条二进制消息为
 * 一帧完整 JPEG：Blob -> ObjectURL -> <img> 渲染，旧 URL 在新帧加载完成后
 * revoke，防止内存泄漏。后端会在新客户端接入时立即下发缓存的最近一帧，
 * 因此通常可以"秒出画面"。
 *
 * 设计定位：纯显示层（pointer-events: none），不承担任何触控 / 坐标职责。
 * 点击、悬停、框选等交互仍由宿主（DeviceStage 静态画布）基于
 * "静态截图 + UI 层级"处理，因此 MJPEG 帧尺寸与 WDA 逻辑坐标系之间的
 * scale 差异不会影响录制正确性——本层只要求帧画面与设备屏幕同宽高比，
 * contain 适配后与底层截图渲染区域自然对齐。
 *
 * 关闭码约定（backend/device_stream/router.py）：
 * - 4005: WDA/MJPEG 上游不可用（reason 含 P1005_WDA_UNAVAILABLE），需人工处理；
 * - 4000: 内部错误，仅提供手动重试；
 * - 1000 reason "stream ended" / 1006 等异常断开: 有限次自动重连后转为手动。
 */
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue'
import { Loading, Refresh } from '@element-plus/icons-vue'

const props = defineProps({
  /** 设备序列号（iOS UDID） */
  serial: {
    type: String,
    required: true
  }
})

const emit = defineEmits(['connected', 'disconnected', 'streaming', 'error', 'wda-unavailable'])

/** 自动重连退避序列：仅对"上游结束 / 异常断开"生效，4005/4000 不自动重试 */
const AUTO_RECONNECT_DELAYS_MS = [1500, 3000, 5000]
/** 帧 URL 回收兜底阈值（img onload 异常缺失时防止无界累积） */
const STALE_URL_FORCE_RELEASE_COUNT = 30

// idle | connecting | reconnecting | streaming | wda-down | ended | error
const status = ref('idle')
const statusDetail = ref('')
const frameUrl = ref('')
const fps = ref(0)

let ws = null
let frameCount = 0
let fpsTimer = null
let reconnectTimer = null
let autoReconnectAttempt = 0
let staleFrameUrls = []

const isWdaDown = computed(() => status.value === 'wda-down')
const isConnectingLike = computed(() => status.value === 'connecting' || status.value === 'reconnecting')
const showBanner = computed(() => ['wda-down', 'ended', 'error'].includes(status.value))
const bannerText = computed(() => {
  if (status.value === 'wda-down') {
    return statusDetail.value || 'WDA 不可用，请到「设备中心」检测/启动 WDA'
  }
  if (status.value === 'ended') {
    return statusDetail.value || '实时画面已断开'
  }
  return statusDetail.value || '实时画面连接异常'
})

// ==================== 帧渲染与 Blob URL 回收 ====================

function releaseStaleFrameUrls() {
  for (const url of staleFrameUrls) {
    try {
      URL.revokeObjectURL(url)
    } catch (err) {
      /* noop */
    }
  }
  staleFrameUrls = []
}

function releaseAllFrameUrls() {
  releaseStaleFrameUrls()
  if (frameUrl.value) {
    try {
      URL.revokeObjectURL(frameUrl.value)
    } catch (err) {
      /* noop */
    }
    frameUrl.value = ''
  }
}

function renderFrame(data) {
  let url = ''
  try {
    url = URL.createObjectURL(new Blob([data], { type: 'image/jpeg' }))
  } catch (err) {
    console.warn('iOS MJPEG 帧渲染失败:', err)
    return
  }

  if (frameUrl.value) {
    staleFrameUrls.push(frameUrl.value)
  }
  frameUrl.value = url
  frameCount++

  if (status.value !== 'streaming') {
    status.value = 'streaming'
    statusDetail.value = ''
    // 收到画面视为链路健康，重置自动重连额度
    autoReconnectAttempt = 0
    emit('streaming')
  }

  if (staleFrameUrls.length > STALE_URL_FORCE_RELEASE_COUNT) {
    releaseStaleFrameUrls()
  }
}

function onFrameLoaded() {
  // 新帧已被 <img> 消费，旧帧 URL 可以安全释放
  releaseStaleFrameUrls()
}

// ==================== FPS 计数 ====================

function startFpsCounter() {
  stopFpsCounter()
  frameCount = 0
  fpsTimer = setInterval(() => {
    fps.value = frameCount
    frameCount = 0
  }, 1000)
}

function stopFpsCounter() {
  if (fpsTimer) {
    clearInterval(fpsTimer)
    fpsTimer = null
  }
  fps.value = 0
}

// ==================== 连接管理 ====================

function clearReconnectTimer() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
}

/** 静默关闭当前连接（摘除回调，避免触发 onclose 重连逻辑） */
function closeSocket() {
  if (!ws) return
  const socket = ws
  ws = null
  socket.onopen = null
  socket.onmessage = null
  socket.onclose = null
  socket.onerror = null
  try {
    socket.close()
  } catch (err) {
    /* noop */
  }
  stopFpsCounter()
}

function connect() {
  if (!props.serial) return
  clearReconnectTimer()
  closeSocket()

  status.value = autoReconnectAttempt > 0 ? 'reconnecting' : 'connecting'
  statusDetail.value = ''

  // WS URL 拼接方式与 ScrcpyPlayer 保持一致（同源 + /ws 前缀，开发态由 vite 代理）
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const wsUrl = `${wsProtocol}//${window.location.host}/ws/ios-mjpeg/${encodeURIComponent(props.serial)}`

  const socket = new WebSocket(wsUrl)
  socket.binaryType = 'arraybuffer'
  ws = socket

  socket.onopen = () => {
    if (ws !== socket) return
    startFpsCounter()
    emit('connected')
  }

  socket.onmessage = (event) => {
    if (ws !== socket) return
    if (event.data instanceof ArrayBuffer) {
      renderFrame(event.data)
    }
  }

  socket.onerror = () => {
    // onclose 总会随后触发，统一在 onclose 中收敛状态
  }

  socket.onclose = (event) => {
    if (ws !== socket) return
    ws = null
    stopFpsCounter()
    emit('disconnected')
    handleClose(event)
  }
}

function handleClose(event) {
  const code = Number(event?.code) || 0
  const reason = String(event?.reason || '')

  // WDA / MJPEG 上游不可用：需要用户到设备中心处理，不做自动重连
  if (code === 4005 || reason.includes('P1005_WDA_UNAVAILABLE')) {
    status.value = 'wda-down'
    statusDetail.value = 'WDA 不可用，请到「设备中心」检测/启动 WDA 后重试'
    emit('wda-unavailable', reason)
    emit('error', reason || 'WDA 不可用')
    return
  }

  // 后端内部错误：仅手动重试
  if (code === 4000) {
    status.value = 'error'
    statusDetail.value = reason ? `实时画面服务异常：${reason}` : '实时画面服务内部错误'
    emit('error', statusDetail.value)
    return
  }

  // 上游正常结束（1000 stream ended）或异常断开（1006 等）：有限次自动重连
  if (autoReconnectAttempt < AUTO_RECONNECT_DELAYS_MS.length) {
    const delay = AUTO_RECONNECT_DELAYS_MS[autoReconnectAttempt]
    autoReconnectAttempt += 1
    status.value = 'reconnecting'
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      connect()
    }, delay)
    return
  }

  status.value = 'ended'
  statusDetail.value = '实时画面已断开，自动重连未成功'
  emit('error', statusDetail.value)
}

function disconnect() {
  clearReconnectTimer()
  closeSocket()
  status.value = 'idle'
  statusDetail.value = ''
}

/** 手动重试：重置自动重连额度后立即重连 */
function retryConnect() {
  autoReconnectAttempt = 0
  connect()
}

// ==================== 生命周期 ====================

onMounted(() => {
  if (props.serial) {
    connect()
  }
})

onUnmounted(() => {
  disconnect()
  releaseAllFrameUrls()
})

// keep-alive 场景：页面失活时断开 WS，激活时恢复
onDeactivated(() => {
  disconnect()
})

onActivated(() => {
  if (props.serial && !ws) {
    autoReconnectAttempt = 0
    connect()
  }
})

watch(() => props.serial, (serial) => {
  // 切换设备：清空旧画面，重新建连
  releaseAllFrameUrls()
  autoReconnectAttempt = 0
  if (serial) {
    connect()
  } else {
    disconnect()
  }
})

defineExpose({
  connect,
  disconnect,
  retryConnect,
  status
})
</script>

<template>
  <div class="ios-mjpeg-player">
    <img
      v-if="frameUrl"
      :src="frameUrl"
      class="mjpeg-frame"
      :class="{ stalled: showBanner || isConnectingLike }"
      draggable="false"
      alt="iOS 实时画面"
      @load="onFrameLoaded"
    />

    <!-- 状态徽标 -->
    <div v-if="isConnectingLike" class="mjpeg-badge">
      <el-icon class="is-loading"><Loading /></el-icon>
      <span>{{ status === 'reconnecting' ? '实时画面重连中…' : '实时画面连接中…' }}</span>
    </div>
    <div v-else-if="status === 'streaming'" class="mjpeg-badge">
      <span class="live-dot"></span>
      <span>实时 {{ fps }} FPS</span>
    </div>

    <!-- 断流横幅：WDA 不可用 / 流结束 / 内部错误 -->
    <div v-if="showBanner" class="mjpeg-banner" :class="{ warning: isWdaDown }">
      <span class="banner-text">{{ bannerText }}</span>
      <el-button size="small" type="primary" :icon="Refresh" @click.stop="retryConnect">
        {{ isWdaDown ? '重新检测' : '重新连接' }}
      </el-button>
    </div>
  </div>
</template>

<style scoped>
/*
 * 本组件是覆盖在静态截图之上的"只读"显示层：
 * 根节点 pointer-events: none，鼠标事件全部穿透到底层画布，
 * 仅断流横幅（含按钮）恢复 pointer-events 以便操作。
 */
.ios-mjpeg-player {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
}

.mjpeg-frame {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  border-radius: 8px;
}

/* 断流 / 重连期间画面置灰，提示当前为冻结帧 */
.mjpeg-frame.stalled {
  filter: grayscale(0.55) brightness(0.75);
}

.mjpeg-badge {
  position: absolute;
  top: 8px;
  left: 8px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border-radius: 999px;
  background: rgba(15, 23, 42, 0.62);
  color: #fff;
  font-size: 12px;
  line-height: 18px;
  z-index: 4;
}

.live-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #4ade80;
  animation: mjpeg-live-pulse 1.6s ease-in-out infinite;
}

@keyframes mjpeg-live-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}

.mjpeg-banner {
  position: absolute;
  top: 12px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 10px;
  max-width: calc(100% - 32px);
  padding: 8px 12px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.97);
  border: 1px solid rgba(245, 108, 108, 0.35);
  box-shadow: 0 10px 24px rgba(15, 23, 42, 0.16);
  pointer-events: auto;
  z-index: 5;
}

.mjpeg-banner.warning {
  border-color: rgba(230, 162, 60, 0.45);
}

.banner-text {
  font-size: 12px;
  color: #303133;
  word-break: break-all;
}
</style>
