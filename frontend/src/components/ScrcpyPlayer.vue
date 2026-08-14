<script setup>
/**
 * ScrcpyPlayer - Scrcpy H.264 视频流播放器组件
 *
 * 通过 WebSocket 接收裸 Annex-B H.264 流并解码渲染：
 * - 主路径：WebCodecs VideoDecoder 逐帧解码后绘制到 <canvas>（低延迟，
 *   拥塞恢复由后端 SPS/PPS→IDR 原子播种保证，对码流不连续天然鲁棒）；
 * - 降级路径：jmuxer + MSE 在 <video> 播放（浏览器不支持 WebCodecs、
 *   目标 codec 不受支持、连续解码失败，或通过 URL query / localStorage
 *   `scrcpyDecoder=jmuxer` 强制指定时启用）。
 * 支持点击触控事件转发、元素悬浮高亮、测试步骤录制，以及只读巡检覆盖层。
 */
import { ref, onMounted, onUnmounted, watch, computed } from 'vue'
import JMuxer from 'jmuxer'
import { ElMessage } from 'element-plus'
import { VideoPlay, VideoPause, Refresh, Loading } from '@element-plus/icons-vue'
import api from '@/api'
import { findBestRecordableNode } from '@/utils/recordableNode'
import { useWebCodecsDecoder, isWebCodecsSupported } from '@/composables/useWebCodecsDecoder'
import { shouldRestoreStoredStreamProfile } from '@/utils/streamProfile'

const props = defineProps({
  /** 设备序列号 */
  serial: {
    type: String,
    default: ''
  },
  /** 可选的认证 WebSocket 地址；传入时不再使用公开设备流地址 */
  streamUrl: {
    type: String,
    default: ''
  },
  /** 是否启用触控转发 */
  touchEnabled: {
    type: Boolean,
    default: true
  },
  /** 严格只读模式；优先级高于 touchEnabled 和 recordMode */
  readOnly: {
    type: Boolean,
    default: false
  },
  /** 挂载后是否自动连接 */
  autoConnect: {
    type: Boolean,
    default: true
  },
  /** 是否显示播放器状态工具栏 */
  showToolbar: {
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
  },
  /** 智能巡检动作覆盖层 */
  overlays: {
    type: Array,
    default: () => []
  },
  /** 覆盖层来源画布宽度，优先于 deviceWidth */
  sourceWidth: {
    type: Number,
    default: 0
  },
  /** 覆盖层来源画布高度，优先于 deviceHeight */
  sourceHeight: {
    type: Number,
    default: 0
  },
  /** 当前聚焦的动作覆盖层 ID */
  selectedOverlayId: {
    type: [String, Number],
    default: ''
  }
})

const emit = defineEmits(['touch', 'error', 'connected', 'disconnected', 'overlay-select'])

// ==================== 响应式状态 ====================

const videoRef = ref(null)
const canvasRef = ref(null)
const playerContentRef = ref(null)
const status = ref('disconnected')
const errorMsg = ref('')
const fps = ref(0)
const hoveredNode = ref(null)
const layoutRevision = ref(0)
const LIVE_EDGE_CHECK_INTERVAL_MS = 1000
const LIVE_EDGE_MAX_LAG_SECONDS = 0.8
const LIVE_EDGE_SEEK_OFFSET_SECONDS = 0.1

const DECODER_WEBCODECS = 'webcodecs'
const DECODER_JMUXER = 'jmuxer'
/** 当前连接使用的解码路径（决定 <canvas> 与 <video> 哪个为活动渲染面） */
const activeDecoder = ref(DECODER_JMUXER)

let jmuxer = null
let webcodecs = null
let ws = null
let frameCount = 0
let fpsTimer = null
let liveEdgeTimer = null
let reconnectTimer = null
let resizeObserver = null
/** WebCodecs 运行期降级标记（连续解码失败 / codec 不支持后置位，切换设备时重置） */
let runtimeFallbackToJmuxer = false

// ==================== 清晰度档位 ====================

const STREAM_PROFILE_OPTIONS = [
  { value: 'hd', label: '高清' },
  { value: 'standard', label: '标准' },
  { value: 'smooth', label: '流畅' }
]
const PROFILE_STORAGE_PREFIX = 'autodroid.scrcpyProfile.'
/** 换档后服务端重启视频流的等待/重试节奏 */
const PROFILE_RECONNECT_DELAY_MS = 1200
const PROFILE_RECONNECT_MAX_RETRIES = 6

const streamProfile = ref('')
const profileSwitching = ref(false)
let profileRetryCount = 0
let profileRetryTimer = null
/** 本次 serial 是否已做过"本地记忆档位 → 服务端"的同步，避免反复自动下发 */
let profileSynced = false

function readStoredProfile() {
  try {
    return window.localStorage?.getItem(PROFILE_STORAGE_PREFIX + props.serial) || ''
  } catch {
    return ''
  }
}

function storeProfile(value) {
  try {
    window.localStorage?.setItem(PROFILE_STORAGE_PREFIX + props.serial, value)
  } catch {
    // localStorage 不可用时档位仅本次会话生效
  }
}

/**
 * 同步服务端档位到下拉框；用户此前选过且与服务端不一致时自动下发一次
 * （覆盖是服务端内存态，平台重启后会回默认档）。
 */
async function syncStreamProfile() {
  if (!props.serial || props.streamUrl) return
  try {
    const { data } = await api.getStreamProfile(props.serial)
    if (!streamProfile.value || !profileSwitching.value) {
      streamProfile.value = data.profile || ''
    }
    const stored = readStoredProfile()
    if (
      !profileSynced
      && STREAM_PROFILE_OPTIONS.some((opt) => opt.value === stored)
      && shouldRestoreStoredStreamProfile(props.serial, stored, data.profile)
    ) {
      profileSynced = true
      applyStreamProfile(stored)
      return
    }
    profileSynced = true
  } catch (err) {
    console.debug('获取投屏档位失败:', err)
  }
}

async function applyStreamProfile(value) {
  profileSwitching.value = true
  streamProfile.value = value
  try {
    await api.setStreamProfile(props.serial, value)
  } catch (err) {
    profileSwitching.value = false
    ElMessage.error('切换清晰度失败: ' + (err.response?.data?.detail || err.message))
    return
  }
  // 服务端正在以新档位重启该设备视频流；稍候重连，未就绪时按次重试
  profileRetryCount = 0
  scheduleProfileReconnect()
}

function scheduleProfileReconnect() {
  clearProfileRetryTimer()
  profileRetryTimer = setTimeout(() => {
    profileRetryTimer = null
    connect()
  }, PROFILE_RECONNECT_DELAY_MS)
}

function clearProfileRetryTimer() {
  if (profileRetryTimer) {
    clearTimeout(profileRetryTimer)
    profileRetryTimer = null
  }
}

function resetProfileState() {
  clearProfileRetryTimer()
  profileSwitching.value = false
  profileRetryCount = 0
  profileSynced = false
  streamProfile.value = ''
}

function onProfileChange(value) {
  storeProfile(value)
  applyStreamProfile(value)
}

// ==================== 解码路径选择 ====================

/**
 * 读取强制解码器配置（调试用）：URL query 优先于 localStorage，键均为 scrcpyDecoder。
 * 仅 'jmuxer' 有强制语义；其余值走自动检测。
 */
function resolveForcedDecoder() {
  try {
    const fromQuery = new URLSearchParams(window.location.search).get('scrcpyDecoder')
    if (fromQuery) return fromQuery
    return window.localStorage?.getItem('scrcpyDecoder') || ''
  } catch {
    return ''
  }
}

function pickDecoder() {
  if (resolveForcedDecoder() === DECODER_JMUXER) return DECODER_JMUXER
  if (runtimeFallbackToJmuxer) return DECODER_JMUXER
  if (!isWebCodecsSupported()) return DECODER_JMUXER
  return DECODER_WEBCODECS
}

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

/** 当前活动渲染面：WebCodecs 路径为 <canvas>，jmuxer 路径为 <video> */
function getActiveSurface() {
  return activeDecoder.value === DECODER_WEBCODECS ? canvasRef.value : videoRef.value
}

/** 读取渲染面的内在像素尺寸（video 为 videoWidth/Height，canvas 为 width/height 属性） */
function getSurfaceIntrinsicSize(surface) {
  if (!surface) return null
  const isVideo = surface.tagName === 'VIDEO'
  const width = isVideo ? surface.videoWidth : surface.width
  const height = isVideo ? surface.videoHeight : surface.height
  if (!width || !height) return null
  return { width, height }
}

/**
 * 计算画面在容器中的实际渲染区域（处理 object-fit: contain 的黑边）
 */
function getVideoRenderArea() {
  // ResizeObserver 通过该依赖触发覆盖层重算。
  void layoutRevision.value
  const surface = getActiveSurface()
  const intrinsic = getSurfaceIntrinsicSize(surface)
  if (!intrinsic) return null

  const rect = surface.getBoundingClientRect()
  const videoAspect = intrinsic.width / intrinsic.height
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
  const explicitWidth = Number(props.sourceWidth || props.deviceWidth) || 0
  const explicitHeight = Number(props.sourceHeight || props.deviceHeight) || 0
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

  return getSurfaceIntrinsicSize(getActiveSurface())
}

function sameOrientation(left, right) {
  if (!left?.width || !left?.height || !right?.width || !right?.height) return false
  const leftRatio = left.width / left.height
  const rightRatio = right.width / right.height
  // Near-square frames do not carry a meaningful rotation signal.
  if (Math.abs(leftRatio - 1) < 0.04 || Math.abs(rightRatio - 1) < 0.04) return true
  return (leftRatio > 1) === (rightRatio > 1)
}

function overlayId(item, index) {
  return String(item?.id ?? item?.action_id ?? item?.action_key ?? item?.key ?? index)
}

function parseBounds(item) {
  const value = item?.bounds
  if (Array.isArray(value) && value.length >= 4) {
    return value.slice(0, 4).map(Number)
  }
  if (value && typeof value === 'object') {
    return [value.x1 ?? value.left, value.y1 ?? value.top, value.x2 ?? value.right, value.y2 ?? value.bottom].map(Number)
  }
  if (typeof value === 'string') {
    const numbers = value.match(/-?\d+(?:\.\d+)?/g)?.slice(0, 4).map(Number)
    if (numbers?.length === 4) return numbers
  }
  return [item?.x1 ?? item?.left, item?.y1 ?? item?.top, item?.x2 ?? item?.right, item?.y2 ?? item?.bottom].map(Number)
}

const normalizedOverlays = computed(() => {
  const coordinateSpace = getCoordinateSpace()
  if (!coordinateSpace) return []
  return (props.overlays || []).map((item, index) => {
    const [rawX1, rawY1, rawX2, rawY2] = parseBounds(item)
    if (![rawX1, rawY1, rawX2, rawY2].every(Number.isFinite)) return null
    const x1 = Math.max(0, Math.min(rawX1, rawX2, coordinateSpace.width))
    const y1 = Math.max(0, Math.min(rawY1, rawY2, coordinateSpace.height))
    const x2 = Math.max(0, Math.min(Math.max(rawX1, rawX2), coordinateSpace.width))
    const y2 = Math.max(0, Math.min(Math.max(rawY1, rawY2), coordinateSpace.height))
    if (x2 <= x1 || y2 <= y1) return null
    const id = overlayId(item, index)
    const statusValue = String(item?.failure_type || item?.final_status || item?.live_status || item?.invocation_status || item?.result || item?.status || 'PENDING').toUpperCase()
    const actionType = String(item?.action_type || item?.type || 'click').toLowerCase()
    const coordinateOnly = Boolean(item?.coordinate_only) || String(item?.locator_type || '').toLowerCase() === 'coordinate'
    const canNumber = ['click', 'input', 'tap'].includes(actionType)
      && !['BLOCKED', 'AMBIGUOUS', 'LOCATOR_AMBIGUOUS', 'LOCATOR_NOT_FOUND', 'LOCATOR_DRIFT', 'COORDINATE_ONLY', 'COORDINATE_UNSAFE', 'COORDINATE_STALE', 'PARENT_RECOVERY_FAILED', 'PARENT_RECOVERY_CASCADE', 'PATH_DIVERGED', 'SKIPPED', 'UNSTABLE_PARENT', 'NOT_REACHED', 'CANCELLED', 'BUDGET_NOT_REACHED', 'COVERED_BY_FAMILY', 'COVERAGE_EXHAUSTED', 'FILTERED_NON_ACTIONABLE', 'QUEUE_TRUNCATED', 'CYCLE_CONVERGED'].includes(statusValue)
      && (!coordinateOnly || ['click', 'tap'].includes(actionType))
    return {
      ...item,
      id,
      x1,
      y1,
      width: x2 - x1,
      height: y2 - y1,
      statusValue,
      actionType,
      coordinateOnly,
      displayOrder: canNumber ? (item?.display_order ?? item?.local_order ?? item?.order ?? index + 1) : null,
      selected: id === String(props.selectedOverlayId ?? '')
    }
  }).filter(Boolean)
})

const svgOverlayStyle = computed(() => {
  if (!normalizedOverlays.value.length) return { display: 'none' }
  const area = getVideoRenderArea()
  const coordinateSpace = getCoordinateSpace()
  const intrinsic = getSurfaceIntrinsicSize(getActiveSurface())
  const containerRect = playerContentRef.value?.getBoundingClientRect()
  // Rotation changes can make the latest event snapshot briefly lag behind
  // the video frame. Hide stale boxes instead of drawing them on the wrong
  // orientation; PAGE_ACTIONS redraws once capture dimensions catch up.
  if (!area || !containerRect || !sameOrientation(coordinateSpace, intrinsic)) {
    return { display: 'none' }
  }
  return {
    display: 'block',
    left: `${area.rect.left - containerRect.left + area.offsetX}px`,
    top: `${area.rect.top - containerRect.top + area.offsetY}px`,
    width: `${area.renderWidth}px`,
    height: `${area.renderHeight}px`
  }
})

const overlayViewBox = computed(() => {
  const size = getCoordinateSpace()
  return size ? `0 0 ${size.width} ${size.height}` : '0 0 1 1'
})

function overlayClass(item) {
  const classes = [`inspection-overlay--${item.statusValue.toLowerCase()}`]
  if (item.coordinateOnly) classes.push('inspection-overlay--coordinate')
  if (item.selected) classes.push('is-selected')
  return classes
}

function scrollArrow(item) {
  if (item.actionType !== 'scroll') return ''
  const direction = String(item.direction || item.scroll_direction || item.label || '').toLowerCase()
  if (direction.includes('down')) return '↓'
  if (direction.includes('left')) return '←'
  if (direction.includes('right')) return '→'
  return '↑'
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
  if (!hoveredNode.value || !getActiveSurface()) return { display: 'none' }

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

/**
 * WebCodecs 管线不可恢复（codec 不支持 / 连续解码失败）时降级：
 * 置位运行期标记并整体重连。后端对新客户端原子播种 SPS/PPS→IDR，
 * 因此重连后 jmuxer 能拿到干净的起始序列，无需在旧流中间拼接。
 */
function handleWebCodecsFallback(reason) {
  if (activeDecoder.value !== DECODER_WEBCODECS) return
  runtimeFallbackToJmuxer = true
  console.warn(`WebCodecs 解码不可用（${reason}），已降级为 jmuxer 兼容模式`)
  ElMessage.warning('硬件解码不可用，已切换至兼容播放模式')
  if (props.streamUrl) {
    // 巡检视频票据为一次性票据，不能复用旧 URL 重连；交给调用方申请新会话。
    emit('error', 'decoder_fallback_requires_new_session')
    disconnect()
    return
  }
  reconnect()
}

function resolveWebSocketUrl() {
  const raw = String(props.streamUrl || '').trim()
  if (!raw) {
    if (!props.serial) return ''
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${protocol}//${window.location.host}/ws/scrcpy/${encodeURIComponent(props.serial)}`
  }
  if (/^wss?:\/\//i.test(raw)) return raw
  const parsed = new URL(raw, window.location.origin)
  parsed.protocol = parsed.protocol === 'https:' ? 'wss:' : 'ws:'
  return parsed.toString()
}

function connect() {
  if (ws) disconnect()

  let wsUrl = ''
  try {
    wsUrl = resolveWebSocketUrl()
  } catch {
    status.value = 'error'
    errorMsg.value = '视频流地址无效'
    emit('error', errorMsg.value)
    return
  }
  if (!wsUrl) {
    status.value = 'error'
    errorMsg.value = '缺少视频流地址'
    emit('error', errorMsg.value)
    return
  }

  status.value = 'connecting'
  errorMsg.value = ''
  activeDecoder.value = pickDecoder()

  if (activeDecoder.value === DECODER_WEBCODECS) {
    webcodecs = useWebCodecsDecoder({
      canvas: canvasRef.value,
      fpsHint: 60,
      debug: import.meta.env.DEV,
      onFrameDecoded: () => {
        frameCount++
        if (frameCount <= 2) layoutRevision.value++
      },
      onFallback: handleWebCodecsFallback
    })
  } else {
    jmuxer = new JMuxer({
      node: videoRef.value,
      mode: 'video',
      flushingTime: 100,
      fps: 60,
      debug: false
    })
  }

  try {
    ws = new WebSocket(wsUrl)
  } catch {
    disconnect()
    status.value = 'error'
    errorMsg.value = '无法创建视频连接'
    emit('error', errorMsg.value)
    return
  }
  ws.binaryType = 'arraybuffer'

  ws.onopen = () => {
    status.value = 'connected'
    profileSwitching.value = false
    profileRetryCount = 0
    emit('connected')
    startFpsCounter()
    // 设备直连模式下同步清晰度档位（异步，不阻塞出流）
    syncStreamProfile()
    // 追赶实时边界仅对 MSE 缓冲播放有意义；WebCodecs 路径逐帧直绘，天然贴近实时
    if (activeDecoder.value === DECODER_JMUXER) {
      startLiveEdgeSync()
    }
  }

  ws.onmessage = (event) => {
    if (!event.data) return
    const bytes = new Uint8Array(event.data)
    if (webcodecs) {
      // FPS 计数由解码输出回调驱动（onFrameDecoded）
      webcodecs.push(bytes)
    } else if (jmuxer) {
      jmuxer.feed({ video: bytes })
      frameCount++
    }
  }

  ws.onerror = () => {
    status.value = 'error'
    errorMsg.value = '连接异常'
    stopLiveEdgeSync()
    emit('error', '连接异常')
  }

  ws.onclose = (event) => {
    status.value = 'disconnected'
    emit('disconnected')
    stopFpsCounter()
    stopLiveEdgeSync()

    // 换档期间服务端会重启视频流：旧连接断开 / 新流未就绪(4004) 都按次重试
    if (profileSwitching.value && profileRetryCount < PROFILE_RECONNECT_MAX_RETRIES) {
      profileRetryCount++
      status.value = 'connecting'
      scheduleProfileReconnect()
      return
    }
    profileSwitching.value = false

    if (event.code === 4004) {
      errorMsg.value = event.reason || '设备未就绪'
      status.value = 'error'
      // 设备直连模式上抛错误，调用方可回落到静态截图等兜底
      if (!props.streamUrl) {
        emit('error', errorMsg.value)
      }
    }
  }
}

function disconnect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  clearProfileRetryTimer()
  stopFpsCounter()
  stopLiveEdgeSync()
  if (ws) {
    ws.onclose = null
    ws.close()
    ws = null
  }
  if (webcodecs) {
    webcodecs.destroy()
    webcodecs = null
  }
  if (jmuxer) {
    jmuxer.destroy()
    jmuxer = null
  }
  status.value = 'disconnected'
}

function reconnect() {
  disconnect()
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    connect()
  }, 300)
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

function keepVideoNearLiveEdge() {
  const video = videoRef.value
  if (!video || status.value !== 'connected') return

  try {
    const buffered = video.buffered
    if (!buffered || buffered.length === 0) return

    const liveEdge = buffered.end(buffered.length - 1)
    const currentTime = Number(video.currentTime || 0)
    if (!Number.isFinite(liveEdge) || !Number.isFinite(currentTime)) return

    const lag = liveEdge - currentTime
    if (lag > LIVE_EDGE_MAX_LAG_SECONDS) {
      video.currentTime = Math.max(0, liveEdge - LIVE_EDGE_SEEK_OFFSET_SECONDS)
    }
  } catch (err) {
    console.debug('播放器追赶实时边界失败:', err)
  }
}

function startLiveEdgeSync() {
  if (liveEdgeTimer) return
  liveEdgeTimer = setInterval(() => {
    keepVideoNearLiveEdge()
  }, LIVE_EDGE_CHECK_INTERVAL_MS)
}

function stopLiveEdgeSync() {
  if (liveEdgeTimer) {
    clearInterval(liveEdgeTimer)
    liveEdgeTimer = null
  }
}

// ==================== 触控事件 ====================

function handleClick(event) {
  if (props.readOnly || !props.touchEnabled || status.value !== 'connected') return

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
  if (typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(() => {
      layoutRevision.value++
    })
    if (playerContentRef.value) resizeObserver.observe(playerContentRef.value)
    if (videoRef.value) resizeObserver.observe(videoRef.value)
    if (canvasRef.value) resizeObserver.observe(canvasRef.value)
  }
  if (props.autoConnect && (props.serial || props.streamUrl)) {
    connect()
  }
})

onUnmounted(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  disconnect()
})

watch(() => [props.serial, props.streamUrl], ([newSerial, newStreamUrl], [oldSerial, oldStreamUrl]) => {
  if (newSerial === oldSerial && newStreamUrl === oldStreamUrl) return
  resetProfileState()
  if (newSerial || newStreamUrl) {
    // 切换设备时重置运行期降级标记：新码流重新走自动检测
    runtimeFallbackToJmuxer = false
    if (props.autoConnect) reconnect()
  } else {
    disconnect()
  }
})

watch(() => [props.sourceWidth, props.sourceHeight, props.deviceWidth, props.deviceHeight, props.overlays], () => {
  layoutRevision.value++
}, { deep: true })

defineExpose({ connect, disconnect, reconnect })
</script>

<template>
  <div class="scrcpy-player">
    <!-- 状态栏 -->
    <div v-if="showToolbar" class="player-toolbar">
      <el-tag :type="statusType" size="small" effect="dark">
        {{ statusText }}
      </el-tag>
      <el-tag v-if="readOnly" type="info" size="small" effect="plain">只读</el-tag>
      <span v-if="status === 'connected'" class="fps-counter">{{ fps }} FPS</span>
      <el-select
        v-if="!streamUrl && serial"
        v-model="streamProfile"
        size="small"
        class="profile-select"
        placeholder="清晰度"
        :disabled="profileSwitching"
        title="投屏清晰度（远程设备建议流畅档）"
        @change="onProfileChange"
      >
        <el-option
          v-for="opt in STREAM_PROFILE_OPTIONS"
          :key="opt.value"
          :label="opt.label"
          :value="opt.value"
        />
      </el-select>
      <div v-if="!streamUrl" class="toolbar-actions">
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
      :class="{ 'is-readonly': readOnly }"
      @click="handleClick"
      @mousemove="onMouseMove"
      @mouseleave="onMouseLeave"
    >
      <video
        v-show="activeDecoder === DECODER_JMUXER"
        ref="videoRef"
        autoplay
        muted
        playsinline
        class="scrcpy-video"
        @loadedmetadata="layoutRevision++"
      ></video>

      <!-- WebCodecs 主路径渲染画布：与 <video> 共用 .scrcpy-video 尺寸/object-fit 行为 -->
      <canvas
        v-show="activeDecoder === DECODER_WEBCODECS"
        ref="canvasRef"
        class="scrcpy-video"
        width="0"
        height="0"
      ></canvas>

      <!-- 元素高亮框 -->
      <div class="hover-overlay" :style="overlayStyle"></div>

      <!-- 智能巡检多动作覆盖层；只响应选中，不向设备发送任何输入。 -->
      <svg
        class="inspection-overlays"
        :style="svgOverlayStyle"
        :viewBox="overlayViewBox"
        preserveAspectRatio="none"
        aria-label="巡检识别控件"
      >
        <g
          v-for="item in normalizedOverlays"
          :key="item.id"
          class="inspection-overlay"
          :class="overlayClass(item)"
          role="button"
          :aria-label="item.label || item.name || `动作 ${item.displayOrder || item.id}`"
          @click.stop="emit('overlay-select', item)"
        >
          <rect
            :x="item.x1"
            :y="item.y1"
            :width="item.width"
            :height="item.height"
            rx="5"
            ry="5"
            vector-effect="non-scaling-stroke"
          />
          <template v-if="item.displayOrder != null">
            <circle :cx="item.x1 + 18" :cy="item.y1 + 18" r="16" vector-effect="non-scaling-stroke" />
            <text :x="item.x1 + 18" :y="item.y1 + 19" class="inspection-overlay__number">{{ item.displayOrder }}</text>
          </template>
          <text
            v-if="scrollArrow(item)"
            :x="item.x1 + item.width / 2"
            :y="item.y1 + item.height / 2"
            class="inspection-overlay__arrow"
          >{{ scrollArrow(item) }}</text>
        </g>
      </svg>

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
          <el-button v-if="!streamUrl" type="primary" size="small" @click="reconnect">重试</el-button>
        </div>
        <div v-else class="overlay-content">
          <el-button v-if="!streamUrl" type="primary" @click="connect">
            <el-icon><VideoPlay /></el-icon>
            <span>开始投屏</span>
          </el-button>
          <span v-else>等待巡检视频流</span>
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

.profile-select {
  width: 86px;
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

.player-content.is-readonly {
  cursor: default;
}

.scrcpy-video {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}

.hover-overlay {
  transition: all 0.1s ease;
}

.inspection-overlays {
  position: absolute;
  z-index: 12;
  overflow: visible;
  pointer-events: none;
}

.inspection-overlay {
  color: #67c23a;
  pointer-events: auto;
  cursor: pointer;
}

.inspection-overlay rect {
  fill: color-mix(in srgb, currentColor 22%, transparent);
  stroke: currentColor;
  stroke-width: 2;
}

.inspection-overlay circle {
  fill: currentColor;
  stroke: #fff;
  stroke-width: 1.5;
}

.inspection-overlay text {
  fill: #fff;
  font-weight: 700;
  text-anchor: middle;
  dominant-baseline: middle;
  paint-order: stroke;
  stroke: rgba(0, 0, 0, 0.46);
  stroke-width: 2px;
  pointer-events: none;
  letter-spacing: 0;
}

.inspection-overlay__number {
  font-size: 18px;
}

.inspection-overlay__arrow {
  font-size: 42px;
}

.inspection-overlay--running,
.inspection-overlay--started,
.inspection-overlay--action_started,
.inspection-overlay--invoking,
.inspection-overlay--active {
  color: #e6a23c;
  animation: inspection-overlay-pulse 0.9s ease-in-out infinite alternate;
}

.inspection-overlay--invoked,
.inspection-overlay--pass,
.inspection-overlay--self_loop,
.inspection-overlay--no_effect,
.inspection-overlay--completed {
  color: #909399;
}

.inspection-overlay--blocked,
.inspection-overlay--dangerous {
  color: #f56c6c;
}

.inspection-overlay--blocked rect,
.inspection-overlay--dangerous rect,
.inspection-overlay--coordinate_only rect,
.inspection-overlay--ambiguous rect,
.inspection-overlay--locator_drift rect,
.inspection-overlay--skipped rect,
.inspection-overlay--unstable_parent rect,
.inspection-overlay--not_reached rect,
.inspection-overlay--coordinate rect {
  stroke-dasharray: 8 5;
}

.inspection-overlay--ambiguous,
.inspection-overlay--locator_drift,
.inspection-overlay--coordinate_only {
  color: #e6a23c;
}

.inspection-overlay--coordinate {
  color: #409eff;
}

.inspection-overlay--no_effect.inspection-overlay--coordinate {
  color: #909399;
}

.inspection-overlay--action_error,
.inspection-overlay--error,
.inspection-overlay--failed {
  color: #f56c6c;
}

.inspection-overlay--skipped,
.inspection-overlay--unstable_parent,
.inspection-overlay--not_reached {
  color: #a8abb2;
}

.inspection-overlay.is-selected rect {
  stroke: #fff;
  stroke-width: 4;
  filter: drop-shadow(0 0 5px #409eff);
}

@keyframes inspection-overlay-pulse {
  from { opacity: 0.62; }
  to { opacity: 1; }
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
