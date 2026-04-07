<script setup>
/**
 * ScrcpyPlayer - Scrcpy H.264 视频流播放器组件
 * 
 * 通过 WebSocket 接收 H.264 原始流，使用 jmuxer 解码后在 <video> 标签中播放。
 * 支持点击触控事件转发、元素悬浮高亮、测试步骤录制。
 */
import { ref, onMounted, onUnmounted, watch, computed } from 'vue'
import JMuxer from 'jmuxer'
import { VideoPlay, VideoPause, Refresh, Loading } from '@element-plus/icons-vue'
import api from '@/api'
import { findBestRecordableNode } from '@/utils/recordableNode'

const props = defineProps({
  /** 设备序列号 */
  serial: {
    type: String,
    required: true
  },
  /** 是否启用触控转发 */
  touchEnabled: {
    type: Boolean,
    default: true
  },
  /** 录制模式：仅 emit touch 事件，不直接发送触控 */
  recordMode: {
    type: Boolean,
    default: false
  },
  /** 设备真实屏幕宽度 */
  deviceWidth: {
    type: Number,
    default: 0
  },
  /** 设备真实屏幕高度 */
  deviceHeight: {
    type: Number,
    default: 0
  },
  /** UI 层级节点（用于元素高亮） */
  nodes: {
    type: Array,
    default: () => []
  }
})

const emit = defineEmits(['touch', 'error', 'connected', 'disconnected'])

// ==================== 响应式状态 ====================

const videoRef = ref(null)
const playerContentRef = ref(null)
const status = ref('disconnected')
const errorMsg = ref('')
const fps = ref(0)
const hoveredNode = ref(null)

let jmuxer = null
let ws = null
let frameCount = 0
let fpsTimer = null

// ==================== 状态计算 ====================

const statusText = computed(() => {
  switch (status.value) {
    case 'disconnected': return '未连接'
    case 'connecting': return '连接中...'
    case 'connected': return '实时投屏'
    case 'error': return `错误: ${errorMsg.value}`
    default: return ''
  }
})

const statusType = computed(() => {
  switch (status.value) {
    case 'connected': return 'success'
    case 'connecting': return 'warning'
    case 'error': return 'danger'
    default: return 'info'
  }
})

// ==================== 坐标映射 ====================

/**
 * 计算视频在容器中的实际渲染区域（处理 object-fit: contain 的黑边）
 */
function getVideoRenderArea() {
  const video = videoRef.value
  if (!video || !video.videoWidth || !video.videoHeight) return null

  const rect = video.getBoundingClientRect()
  const videoAspect = video.videoWidth / video.videoHeight
  const containerAspect = rect.width / rect.height

  let renderWidth, renderHeight, offsetX, offsetY

  if (videoAspect > containerAspect) {
    renderWidth = rect.width
    renderHeight = rect.width / videoAspect
    offsetX = 0
    offsetY = (rect.height - renderHeight) / 2
  } else {
    renderHeight = rect.height
    renderWidth = rect.height * videoAspect
    offsetX = (rect.width - renderWidth) / 2
    offsetY = 0
  }

  return { rect, renderWidth, renderHeight, offsetX, offsetY }
}

function getCoordinateSpace() {
  const explicitWidth = Number(props.deviceWidth) || 0
  const explicitHeight = Number(props.deviceHeight) || 0
  if (explicitWidth > 0 && explicitHeight > 0) {
    return { width: explicitWidth, height: explicitHeight }
  }

  let maxX = 0
  let maxY = 0
  for (const node of props.nodes) {
    maxX = Math.max(maxX, Number(node?.x2) || 0)
    maxY = Math.max(maxY, Number(node?.y2) || 0)
  }
  if (maxX > 0 && maxY > 0) {
    return { width: maxX, height: maxY }
  }

  const video = videoRef.value
  if (video?.videoWidth && video?.videoHeight) {
    return { width: video.videoWidth, height: video.videoHeight }
  }

  return null
}

/**
 * 将鼠标客户端坐标映射为设备真实坐标
 */
function mapToDeviceCoords(clientX, clientY) {
  const area = getVideoRenderArea()
  const target = getCoordinateSpace()
  if (!area || !target) return null

  const { rect, renderWidth, renderHeight, offsetX, offsetY } = area

  const clickX = clientX - rect.left - offsetX
  const clickY = clientY - rect.top - offsetY

  if (clickX < 0 || clickY < 0 || clickX > renderWidth || clickY > renderHeight) return null

  return {
    x: Math.min(target.width - 1, Math.max(0, Math.round((clickX / renderWidth) * target.width))),
    y: Math.min(target.height - 1, Math.max(0, Math.round((clickY / renderHeight) * target.height))),
    // 提供在渲染区域中的相对位置（用于高亮计算）
    relX: clickX / renderWidth,
    relY: clickY / renderHeight
  }
}

// ==================== 元素高亮 ====================

function findNodeAt(realX, realY) {
  return findBestRecordableNode(props.nodes, realX, realY)
}

function onMouseMove(event) {
  if (props.nodes.length === 0) {
    hoveredNode.value = null
    return
  }
  const coords = mapToDeviceCoords(event.clientX, event.clientY)
  if (!coords) {
    hoveredNode.value = null
    return
  }
  hoveredNode.value = findNodeAt(coords.x, coords.y)
}

function onMouseLeave() {
  hoveredNode.value = null
}

const overlayStyle = computed(() => {
  if (!hoveredNode.value || !videoRef.value) return { display: 'none' }

  const area = getVideoRenderArea()
  const target = getCoordinateSpace()
  if (!area || !target) return { display: 'none' }

  const { rect, renderWidth, renderHeight, offsetX, offsetY } = area
  const containerRect = playerContentRef.value?.getBoundingClientRect()
  if (!containerRect) return { display: 'none' }

  const node = hoveredNode.value
  const scaleX = renderWidth / target.width
  const scaleY = renderHeight / target.height

  // 视频元素相对于 player-content 的偏移
  const videoOffsetX = rect.left - containerRect.left + offsetX
  const videoOffsetY = rect.top - containerRect.top + offsetY

  return {
    display: 'block',
    position: 'absolute',
    left: `${videoOffsetX + node.x1 * scaleX}px`,
    top: `${videoOffsetY + node.y1 * scaleY}px`,
    width: `${(node.x2 - node.x1) * scaleX}px`,
    height: `${(node.y2 - node.y1) * scaleY}px`,
    border: '2px solid #e74c3c',
    backgroundColor: 'rgba(231, 76, 60, 0.15)',
    pointerEvents: 'none',
    boxSizing: 'border-box',
    borderRadius: '4px',
    zIndex: 10
  }
})

// ==================== 连接管理 ====================

function connect() {
  if (ws) disconnect()

  status.value = 'connecting'
  errorMsg.value = ''

  jmuxer = new JMuxer({
    node: videoRef.value,
    mode: 'video',
    flushingTime: 0,
    fps: 60,
    debug: false
  })

  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const wsUrl = `${wsProtocol}//${window.location.host}/ws/scrcpy/${props.serial}`

  ws = new WebSocket(wsUrl)
  ws.binaryType = 'arraybuffer'

  ws.onopen = () => {
    status.value = 'connected'
    emit('connected')
    startFpsCounter()
  }

  ws.onmessage = (event) => {
    if (jmuxer && event.data) {
      jmuxer.feed({
        video: new Uint8Array(event.data)
      })
      frameCount++
    }
  }

  ws.onerror = () => {
    status.value = 'error'
    errorMsg.value = '连接异常'
    emit('error', '连接异常')
  }

  ws.onclose = (event) => {
    status.value = 'disconnected'
    emit('disconnected')
    stopFpsCounter()

    if (event.code === 4004) {
      errorMsg.value = event.reason || '设备未就绪'
      status.value = 'error'
    }
  }
}

function disconnect() {
  stopFpsCounter()
  if (ws) {
    ws.onclose = null
    ws.close()
    ws = null
  }
  if (jmuxer) {
    jmuxer.destroy()
    jmuxer = null
  }
  status.value = 'disconnected'
}

function reconnect() {
  disconnect()
  setTimeout(() => connect(), 300)
}

// ==================== FPS 计数器 ====================

function startFpsCounter() {
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

// ==================== 触控事件 ====================

function handleClick(event) {
  if (!props.touchEnabled || status.value !== 'connected') return

  const coords = mapToDeviceCoords(event.clientX, event.clientY)
  if (!coords) return

  emit('touch', { x: coords.x, y: coords.y, action: 0, relX: coords.relX, relY: coords.relY })

  // 录制模式下不直接发送触控，由父组件通过 API 统一处理
  if (!props.recordMode) {
    api.sendTouch(props.serial, 0, coords.x, coords.y).catch(err => {
      console.error('触控事件失败:', err)
    })
  }
}

// ==================== 生命周期 ====================

onMounted(() => {
  if (props.serial) {
    connect()
  }
})

onUnmounted(() => {
  disconnect()
})

watch(() => props.serial, (newSerial) => {
  if (newSerial) {
    reconnect()
  } else {
    disconnect()
  }
})
</script>

<template>
  <div class="scrcpy-player">
    <!-- 状态栏 -->
    <div class="player-toolbar">
      <el-tag :type="statusType" size="small" effect="dark">
        {{ statusText }}
      </el-tag>
      <span v-if="status === 'connected'" class="fps-counter">{{ fps }} FPS</span>
      <div class="toolbar-actions">
        <el-button
          v-if="status === 'disconnected' || status === 'error'"
          :icon="VideoPlay"
          circle
          size="small"
          type="success"
          @click="connect"
          title="连接"
        />
        <el-button
          v-if="status === 'connected'"
          :icon="VideoPause"
          circle
          size="small"
          type="danger"
          @click="disconnect"
          title="断开"
        />
        <el-button
          :icon="Refresh"
          circle
          size="small"
          @click="reconnect"
          title="重连"
        />
      </div>
    </div>

    <!-- 视频区域 -->
    <div
      ref="playerContentRef"
      class="player-content"
      @click="handleClick"
      @mousemove="onMouseMove"
      @mouseleave="onMouseLeave"
    >
      <video
        ref="videoRef"
        autoplay
        muted
        playsinline
        class="scrcpy-video"
      ></video>

      <!-- 元素高亮框 -->
      <div class="hover-overlay" :style="overlayStyle"></div>

      <!-- 元素信息提示 -->
      <div v-if="hoveredNode" class="element-tooltip">
        <div v-if="hoveredNode.text" class="tip-row"><b>Text:</b> {{ hoveredNode.text }}</div>
        <div v-if="hoveredNode.resourceId" class="tip-row"><b>ID:</b> {{ hoveredNode.resourceId }}</div>
        <div v-if="hoveredNode.contentDesc" class="tip-row"><b>Desc:</b> {{ hoveredNode.contentDesc }}</div>
        <div class="tip-row"><b>Class:</b> {{ hoveredNode.className }}</div>
      </div>

      <!-- 未连接提示 -->
      <div v-if="status !== 'connected'" class="player-overlay">
        <div v-if="status === 'connecting'" class="overlay-content">
          <el-icon class="is-loading" :size="32"><Loading /></el-icon>
          <p>正在连接设备...</p>
        </div>
        <div v-else-if="status === 'error'" class="overlay-content error">
          <p>{{ errorMsg }}</p>
          <el-button type="primary" size="small" @click="reconnect">重试</el-button>
        </div>
        <div v-else class="overlay-content">
          <el-button type="primary" @click="connect">
            <el-icon><VideoPlay /></el-icon>
            <span>开始投屏</span>
          </el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.scrcpy-player {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: transparent;
  border-radius: 8px;
  overflow: hidden;
}

.player-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  background: #fff;
  color: #606266;
  border-bottom: 1px solid #e4e7ed;
}

.fps-counter {
  font-size: 12px;
  color: #4ecca3;
  font-family: 'Courier New', monospace;
}

.toolbar-actions {
  margin-left: auto;
  display: flex;
  gap: 4px;
}

.player-content {
  flex: 1;
  position: relative;
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 400px;
  cursor: crosshair;
}

.scrcpy-video {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}

.hover-overlay {
  transition: all 0.1s ease;
}

.element-tooltip {
  position: absolute;
  bottom: 20px;
  left: 20px;
  background: #fff;
  color: #303133;
  padding: 12px 16px;
  border-radius: 8px;
  font-size: 12px;
  max-width: 350px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.1);
  border: 1px solid #e4e7ed;
  z-index: 20;
  pointer-events: none;
}

.tip-row {
  margin-bottom: 4px;
  word-break: break-all;
}

.tip-row:last-child {
  margin-bottom: 0;
}

.player-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  justify-content: center;
  align-items: center;
  background: rgba(26, 26, 46, 0.85);
}

.overlay-content {
  text-align: center;
  color: #e0e0e0;
}

.overlay-content.error {
  color: #f56c6c;
}

.overlay-content p {
  margin: 10px 0;
}
</style>
