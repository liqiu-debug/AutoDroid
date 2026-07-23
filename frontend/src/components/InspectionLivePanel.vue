<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  Aim,
  Clock,
  Lock,
  Refresh,
  WarningFilled,
} from '@element-plus/icons-vue'
import api from '@/api'
import ScrcpyPlayer from '@/components/ScrcpyPlayer.vue'
import {
  inspectionActionStatus,
  inspectionActionStatusMeta,
  inspectionExecutionDispositionLabel,
  inspectionFallbackImageReady,
  inspectionLiveActionPanel,
  inspectionLiveCanvasMatchesPanel,
  inspectionLivePanelEpoch,
  inspectionLivePanelOwnerId,
  inspectionPageDisplayName,
  inspectionPhaseLabel,
  isInspectionVerificationPhase,
  shouldClearInspectionActionOverlay,
  INSPECTION_LIVE_SNAPSHOT_EVENT_TYPES,
  mergeInspectionLiveSnapshot,
  NON_NUMBERED_INSPECTION_ACTION_STATUSES,
} from '@/utils/inspectionPresentation'

const props = defineProps({
  runId: {
    type: [Number, String],
    required: true,
  },
  runStatus: {
    type: String,
    default: '',
  },
  /** 页签不可见时设为 false，组件会立即释放两个 WebSocket。 */
  active: {
    type: Boolean,
    default: true,
  },
  /** 传入状态 ID 时进入历史动作地图模式，不连接实时视频。 */
  stateId: {
    type: [Number, String],
    default: '',
  },
  fallbackScreenshotPath: {
    type: String,
    default: '',
  },
  fallbackScreenshotAssetId: {
    type: [Number, String],
    default: '',
  },
  pageLabel: {
    type: String,
    default: '',
  },
})

const emit = defineEmits(['snapshot', 'action-select', 'state-select', 'terminal', 'error'])

const TERMINAL_STATUSES = new Set(['PASS', 'WARNING', 'FAIL', 'ERROR', 'ABORTED'])
const RUNNING_ACTION_STATUSES = new Set(['RUNNING', 'STARTED', 'ACTION_STARTED', 'INVOKING', 'ACTIVE'])
const PENDING_ACTION_STATUSES = new Set(['PENDING', 'QUEUED', 'READY', 'NOT_STARTED'])

const snapshot = ref({})
const historicalMap = ref(null)
const sessionState = ref('idle')
const eventConnected = ref(false)
const videoStreamUrl = ref('')
const videoFailed = ref(false)
const selectedActionId = ref('')
const actionFilter = ref('all')
const localEvents = ref([])
const fallbackImageUrl = ref('')
const fallbackImageSourcePath = ref('')
const fallbackImageNaturalSize = ref({ width: 0, height: 0 })
const lastError = ref('')
const loading = ref(false)

let eventSocket = null
let retryTimer = null
let pollTimer = null
let requestGeneration = 0
let retryCount = 0
let fallbackObjectUrl = ''
let fallbackRequestGeneration = 0
let terminalEmitted = false
let lastEmittedStateId = null

const historicalMode = computed(() => props.stateId !== '' && props.stateId != null)
const liveActionPanel = computed(() => inspectionLiveActionPanel(snapshot.value || {}))
const effectiveData = computed(() => historicalMap.value || liveActionPanel.value || {})
const effectiveStatus = computed(() => {
  const propStatus = String(props.runStatus || '').toUpperCase()
  if (TERMINAL_STATUSES.has(propStatus)) return propStatus
  return String(snapshot.value?.run_status || snapshot.value?.status || propStatus || 'PENDING').toUpperCase()
})
const isTerminal = computed(() => Boolean(snapshot.value?.terminal) || TERMINAL_STATUSES.has(effectiveStatus.value))
const shouldStream = computed(() => props.active && !historicalMode.value && !isTerminal.value)

function valueFrom(data, ...keys) {
  for (const key of keys) {
    if (data?.[key] !== undefined && data?.[key] !== null && data?.[key] !== '') return data[key]
  }
  return undefined
}

function parseBounds(value, action = {}) {
  if (Array.isArray(value) && value.length >= 4) return value.slice(0, 4).map(Number)
  if (value && typeof value === 'object') {
    return [value.x1 ?? value.left, value.y1 ?? value.top, value.x2 ?? value.right, value.y2 ?? value.bottom].map(Number)
  }
  if (typeof value === 'string') {
    const numbers = value.match(/-?\d+(?:\.\d+)?/g)?.slice(0, 4).map(Number)
    if (numbers?.length === 4) return numbers
  }
  return [action.x1 ?? action.left, action.y1 ?? action.top, action.x2 ?? action.right, action.y2 ?? action.bottom].map(Number)
}

function normalizeAction(action, index, fallbackDisplayOrder) {
  const status = inspectionActionStatus(action)
  const bounds = parseBounds(action?.bounds, action)
  const id = String(valueFrom(action, 'id', 'action_id', 'action_key', 'key') ?? index)
  const actionType = String(valueFrom(action, 'action_type', 'type') || 'click').toLowerCase()
  const label = String(valueFrom(action, 'label', 'display_label', 'target_label', 'name') || actionType).slice(0, 48)
  const coordinateOnly = Boolean(action?.coordinate_only) || String(valueFrom(action, 'locator_type', 'locator_method', 'locator_by', 'by') || '').toLowerCase() === 'coordinate'
  const numberable = ['click', 'input', 'tap'].includes(actionType)
    && !NON_NUMBERED_INSPECTION_ACTION_STATUSES.has(status)
    && (!coordinateOnly || ['click', 'tap'].includes(actionType))
  return {
    id,
    bounds,
    x1: bounds[0],
    y1: bounds[1],
    x2: bounds[2],
    y2: bounds[3],
    status,
    action_type: actionType,
    label,
    display_order: numberable ? (valueFrom(action, 'display_order', 'local_order') ?? fallbackDisplayOrder) : null,
    page_order: valueFrom(action, 'page_order', 'order') ?? index + 1,
    global_sequence: valueFrom(action, 'global_sequence', 'sequence', 'transition_sequence'),
    locator_type: valueFrom(action, 'locator_type', 'locator_method', 'locator_by', 'by'),
    coordinate_only: coordinateOnly,
    direction: String(valueFrom(action, 'direction', 'scroll_direction') || ''),
    risk: String(valueFrom(action, 'risk', 'risk_level') || ''),
    reason: String(valueFrom(action, 'reason', 'blocked_reason', 'risk_reason', 'error', 'error_message') || ''),
    execution_disposition: String(valueFrom(action, 'execution_disposition', 'disposition') || ''),
    failure_type: String(valueFrom(action, 'failure_type') || ''),
    coverage_source_transition_id: valueFrom(action, 'coverage_source_transition_id'),
    coverage_contract_id: valueFrom(action, 'coverage_contract_id'),
    action_group_key: valueFrom(action, 'action_group_key'),
    sampling_disposition: valueFrom(action, 'sampling_disposition'),
  }
}

const actions = computed(() => {
  const raw = Array.isArray(effectiveData.value) ? effectiveData.value : (effectiveData.value?.actions || [])
  const current = !historicalMap.value && effectiveData.value?.current_action
  const currentKey = valueFrom(current, 'action_id', 'action_key', 'id')
  let fallbackOrder = 0
  return raw.map((rawAction, index) => {
    const rawKey = valueFrom(rawAction, 'action_id', 'action_key', 'id')
    const action = currentKey != null && String(rawKey) === String(currentKey)
      ? { ...rawAction, ...current }
      : rawAction
    const status = inspectionActionStatus(action)
    const type = String(valueFrom(action, 'action_type', 'type') || 'click').toLowerCase()
    const coordinateOnly = Boolean(action?.coordinate_only) || String(valueFrom(action, 'locator_type', 'locator_method', 'locator_by', 'by') || '').toLowerCase() === 'coordinate'
    const numberable = ['click', 'input', 'tap'].includes(type)
      && !NON_NUMBERED_INSPECTION_ACTION_STATUSES.has(status)
      && (!coordinateOnly || ['click', 'tap'].includes(type))
    if (numberable) fallbackOrder++
    return normalizeAction(action, index, numberable ? fallbackOrder : null)
  })
})

const filteredActions = computed(() => actions.value.filter(action => {
  if (actionFilter.value === 'pending') return PENDING_ACTION_STATUSES.has(action.status)
  if (actionFilter.value === 'current') return RUNNING_ACTION_STATUSES.has(action.status)
  return true
}))

const effectivePage = computed(() => historicalMap.value || liveActionPanel.value?.page || {})
const sourceWidth = computed(() => Number(valueFrom(effectivePage.value, 'screen_width', 'source_width', 'width') || effectivePage.value?.source_size?.width || 0))
const sourceHeight = computed(() => Number(valueFrom(effectivePage.value, 'screen_height', 'source_height', 'height') || effectivePage.value?.source_size?.height || 0))
const overlaySourceWidth = computed(() => sourceWidth.value || fallbackImageNaturalSize.value.width || 0)
const overlaySourceHeight = computed(() => sourceHeight.value || fallbackImageNaturalSize.value.height || 0)
const deviceSerial = computed(() => String(valueFrom(snapshot.value, 'device_serial', 'serial') || ''))
const rawPhase = computed(() => valueFrom(snapshot.value, 'current_phase', 'phase', 'current_stage', 'stage', 'run_stage'))
const stage = computed(() => isTerminal.value && !rawPhase.value ? '已结束' : inspectionPhaseLabel(rawPhase.value))
const frontier = computed(() => snapshot.value?.frontier || snapshot.value?.progress?.frontier || {})
const frontierCounts = computed(() => {
  const source = { ...snapshot.value, ...snapshot.value?.progress, ...frontier.value }
  const value = (...keys) => {
    const found = valueFrom(source, ...keys)
    return found === undefined ? null : Number(found)
  }
  return {
    queued: value('queued_count', 'queued_states', 'queued'),
    deferred: value('deferred_count', 'deferred_states', 'deferred'),
    pending: value('pending_action_count', 'pending_actions', 'pending'),
  }
})
const verificationPhase = computed(() => shouldClearInspectionActionOverlay(
  rawPhase.value,
  snapshot.value?.current_stage,
))
const progressText = computed(() => {
  const progress = snapshot.value?.progress || snapshot.value
  const current = valueFrom(progress, 'actions_finished', 'processed_actions', 'action_index', 'processed')
  const total = valueFrom(progress, 'actions_total', 'total_actions', 'action_count', 'total')
  return current != null && total != null ? `${current}/${total}` : '-'
})
const currentActionLabel = computed(() => {
  const current = effectiveData.value?.current_action
  return String(valueFrom(current, 'label', 'display_label', 'target_label', 'name') || '等待下一步')
})
const currentPageLabel = computed(() => inspectionPageDisplayName(effectivePage.value, props.pageLabel))
const fallbackImageReady = computed(() => inspectionFallbackImageReady(
  fallbackImageUrl.value,
  fallbackImageSourcePath.value,
  screenshotPath(),
))
const storedPanelImageVisible = computed(() => fallbackImageReady.value
  && (!shouldStream.value || !videoStreamUrl.value || videoFailed.value))
const canvasMatchesPanel = computed(() => historicalMode.value
  || storedPanelImageVisible.value
  || inspectionLiveCanvasMatchesPanel(snapshot.value))
const overlayVisible = computed(() => !verificationPhase.value
  && canvasMatchesPanel.value
  && (historicalMode.value || isTerminal.value || snapshot.value?.overlay_visible !== false))
const playerOverlays = computed(() => overlayVisible.value ? actions.value : [])
const eventItems = computed(() => {
  const fromSnapshot = Array.isArray(snapshot.value?.recent_events)
    ? snapshot.value.recent_events
    : Array.isArray(snapshot.value?.events) ? snapshot.value.events : []
  return [...fromSnapshot, ...localEvents.value].slice(-20).reverse()
})
const staticOverlayReady = computed(() => overlaySourceWidth.value > 0 && overlaySourceHeight.value > 0)
const staticViewBox = computed(() => staticOverlayReady.value
  ? `0 0 ${overlaySourceWidth.value} ${overlaySourceHeight.value}`
  : '0 0 1 1')

function statusMeta(status) {
  return inspectionActionStatusMeta(status)
}

function actionIcon(action) {
  if (action.status === 'BLOCKED') return Lock
  if (action.action_type === 'scroll') return Refresh
  if (['ERROR', 'ACTION_ERROR'].includes(action.status)) return WarningFilled
  return Aim
}

function actionArrow(action) {
  if (action.action_type !== 'scroll') return ''
  const direction = action.direction.toLowerCase()
  if (direction.includes('down')) return '↓'
  if (direction.includes('left')) return '←'
  if (direction.includes('right')) return '→'
  return '↑'
}

function eventText(event) {
  if (typeof event === 'string') return event.slice(0, 120)
  const type = String(valueFrom(event, 'type', 'event_type') || 'EVENT')
  const status = valueFrom(event, 'run_status', 'status', 'result')
  const label = valueFrom(event, 'label', 'message', 'current_stage', 'stage', 'phase')
  return [type, label, status].filter(Boolean).join(' · ').slice(0, 160)
}

function eventTime(event) {
  const value = valueFrom(event, 'timestamp', 'created_at', 'emitted_at', 'at', 'time')
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString([], { hour12: false })
}

function websocketUrl(rawUrl, ticket, fallbackPath) {
  const path = String(rawUrl || fallbackPath || '').trim()
  if (!path) return ''
  const url = /^wss?:\/\//i.test(path) ? new URL(path) : new URL(path, window.location.origin)
  if (!/^wss?:$/i.test(url.protocol)) url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  if (ticket && !url.searchParams.has('ticket')) url.searchParams.set('ticket', ticket)
  return url.toString()
}

function sessionValue(data, groupName, key) {
  return data?.[groupName]?.[key] ?? data?.[`${groupName}_${key}`] ?? data?.[key]
}

function setSnapshot(next) {
  if (!next || typeof next !== 'object' || Array.isArray(next)) return
  if (next.run_id != null && String(next.run_id) !== String(props.runId)) return
  const merged = mergeInspectionLiveSnapshot(snapshot.value, next)
  if (merged === snapshot.value) return
  const previousOwnerId = inspectionLivePanelOwnerId(snapshot.value)
  const previousEpoch = inspectionLivePanelEpoch(snapshot.value)
  snapshot.value = merged
  const nextOwnerId = inspectionLivePanelOwnerId(snapshot.value)
  const nextEpoch = inspectionLivePanelEpoch(snapshot.value)
  const ownerChanged = String(previousOwnerId ?? '') !== String(nextOwnerId ?? '')
  const epochChanged = previousEpoch !== nextEpoch
  if (ownerChanged || epochChanged) {
    selectedActionId.value = ''
    clearFallbackImage()
  }
  const clearActionHighlight = shouldClearInspectionActionOverlay(
    valueFrom(snapshot.value, 'current_phase', 'phase'),
    valueFrom(snapshot.value, 'current_stage', 'stage', 'run_stage'),
  )
  if (clearActionHighlight) {
    selectedActionId.value = ''
    snapshot.value.overlay_visible = false
  }
  emit('snapshot', snapshot.value)
  if (
    nextOwnerId != null
    && String(nextOwnerId) !== String(lastEmittedStateId ?? '')
  ) {
    lastEmittedStateId = nextOwnerId
    emit('state-select', nextOwnerId)
  }
  const currentActionId = valueFrom(
    inspectionLiveActionPanel(snapshot.value)?.current_action,
    'action_id',
    'action_key',
    'id',
  )
  if (!clearActionHighlight && currentActionId != null) {
    selectedActionId.value = String(currentActionId)
  }
  if ((snapshot.value?.terminal || TERMINAL_STATUSES.has(String(snapshot.value?.run_status || snapshot.value?.status || '').toUpperCase())) && !terminalEmitted) {
    terminalEmitted = true
    emit('terminal', snapshot.value)
  }
}

function appendEvent(event) {
  if (!event) return
  localEvents.value = [...localEvents.value, event].slice(-20)
}

function applyLivePayload(data) {
  if (!data || typeof data !== 'object') return
  const completeSnapshot = data.run_id != null || data.revision != null
  if (data.snapshot && typeof data.snapshot === 'object') setSnapshot(data.snapshot)
  else if (completeSnapshot || (!data.type && !data.event_type)) setSnapshot(data)

  if (completeSnapshot && Array.isArray(data.recent_events)) localEvents.value = []

  const event = data.event || ((data.type || data.event_type) ? data : null)
  if (!event) return
  if (!completeSnapshot || !Array.isArray(data.recent_events)) appendEvent(event)
  const payload = event.payload && typeof event.payload === 'object' ? event.payload : event
  const eventType = String(valueFrom(event, 'type', 'event_type') || '').toUpperCase()
  if (eventType === 'PAGE_ACTIONS') {
    setSnapshot({ ...payload, actions: payload.actions || event.actions || [] , overlay_visible: true })
  } else if (eventType === 'OVERLAY_CLEAR') {
    setSnapshot({ ...payload, overlay_visible: false })
  } else if (eventType === 'TERMINAL') {
    setSnapshot({ ...payload, status: payload.status || event.status })
  } else if (eventType === 'PHASE_CHANGED') {
    const phase = valueFrom(payload, 'current_phase', 'phase', 'current_stage', 'stage')
    setSnapshot({ ...payload, overlay_visible: !isInspectionVerificationPhase(phase) })
    if (isInspectionVerificationPhase(phase)) selectedActionId.value = ''
  } else if (INSPECTION_LIVE_SNAPSHOT_EVENT_TYPES.has(eventType)) {
    setSnapshot(payload)
  }
}

function closeEventSocket() {
  if (!eventSocket) return
  eventSocket.onopen = null
  eventSocket.onmessage = null
  eventSocket.onerror = null
  eventSocket.onclose = null
  eventSocket.close()
  eventSocket = null
  eventConnected.value = false
}

function clearTimers() {
  if (retryTimer) window.clearTimeout(retryTimer)
  if (pollTimer) window.clearInterval(pollTimer)
  retryTimer = null
  pollTimer = null
}

function stopLiveSession() {
  requestGeneration++
  clearTimers()
  closeEventSocket()
  videoStreamUrl.value = ''
  sessionState.value = 'idle'
}

async function refreshSnapshot(quiet = true) {
  const generation = requestGeneration
  const runId = String(props.runId)
  try {
    const response = await api.getInspectionLive(runId)
    if (generation !== requestGeneration || runId !== String(props.runId)) return null
    applyLivePayload(response.data?.snapshot || response.data)
    return response.data
  } catch (error) {
    if (!quiet) throw error
    return null
  }
}

function startPolling() {
  if (pollTimer || !props.active) return
  pollTimer = window.setInterval(async () => {
    await refreshSnapshot(true)
    if (isTerminal.value) {
      stopLiveSession()
      await loadFallbackImage()
    }
  }, 2000)
}

function scheduleSessionRetry() {
  if (!shouldStream.value || retryTimer || retryCount >= 3) {
    startPolling()
    return
  }
  retryCount++
  retryTimer = window.setTimeout(() => {
    retryTimer = null
    startLiveSession(true)
  }, Math.min(4000, 800 * (2 ** (retryCount - 1))))
}

function openEventSocket(url) {
  closeEventSocket()
  if (!url) {
    startPolling()
    return
  }
  const socket = new WebSocket(url)
  eventSocket = socket
  socket.onopen = () => {
    if (eventSocket !== socket) return
    eventConnected.value = true
    lastError.value = ''
    retryCount = 0
  }
  socket.onmessage = event => {
    if (eventSocket !== socket || typeof event.data !== 'string') return
    try {
      applyLivePayload(JSON.parse(event.data))
      if (isTerminal.value) {
        stopLiveSession()
        loadFallbackImage()
      }
    } catch {
      // 非 JSON 消息不是实时事件协议的一部分，安全忽略。
    }
  }
  socket.onerror = () => {
    if (eventSocket !== socket) return
    eventConnected.value = false
  }
  socket.onclose = () => {
    if (eventSocket !== socket) return
    eventSocket = null
    eventConnected.value = false
    scheduleSessionRetry()
  }
}

async function startLiveSession(isRetry = false) {
  if (!shouldStream.value) return
  const generation = ++requestGeneration
  clearTimers()
  closeEventSocket()
  videoStreamUrl.value = ''
  videoFailed.value = false
  sessionState.value = 'connecting'
  if (!isRetry) loading.value = true

  try {
    await refreshSnapshot(true)
    if (generation !== requestGeneration || !shouldStream.value) return
    const response = await api.createInspectionLiveSession(props.runId)
    if (generation !== requestGeneration || !shouldStream.value) return
    const data = response.data || {}
    applyLivePayload(data.snapshot)

    const liveTicket = sessionValue(data, 'live', 'ticket') || data.event_ticket
    const videoTicket = sessionValue(data, 'video', 'ticket')
    const liveUrl = sessionValue(data, 'live', 'url') || data.event_ws_url || data.event_url || data.live_url
    const rawVideoUrl = sessionValue(data, 'video', 'url') || data.video_ws_url || data.video_url
    const runPath = encodeURIComponent(String(props.runId))
    const eventUrl = websocketUrl(liveUrl, liveTicket, `/ws/inspections/runs/${runPath}/live`)
    videoStreamUrl.value = data.video_available === false
      ? ''
      : websocketUrl(rawVideoUrl, videoTicket, `/ws/inspections/runs/${runPath}/video`)
    openEventSocket(eventUrl)
    sessionState.value = 'connected'
  } catch (error) {
    if (generation !== requestGeneration) return
    sessionState.value = 'error'
    lastError.value = error.response?.data?.detail || error.message || '实时巡检连接失败'
    emit('error', lastError.value)
    scheduleSessionRetry()
  } finally {
    if (generation === requestGeneration) loading.value = false
  }
}

async function loadActionMap() {
  historicalMap.value = null
  if (!historicalMode.value || !props.runId) return
  loading.value = true
  try {
    const response = await api.getInspectionActionMap(props.runId, props.stateId)
    historicalMap.value = Array.isArray(response.data) ? { actions: response.data } : (response.data || {})
    await loadFallbackImage()
  } catch (error) {
    lastError.value = error.response?.data?.detail || error.message || '加载动作地图失败'
    emit('error', lastError.value)
  } finally {
    loading.value = false
  }
}

function screenshotSource() {
  const assetId = valueFrom(
    effectivePage.value,
    'screenshot_asset_id',
    'thumbnail_asset_id',
    'representative_screenshot_asset_id',
  ) || ((historicalMode.value || isTerminal.value) ? props.fallbackScreenshotAssetId : '')
  if (assetId !== '' && assetId !== null && assetId !== undefined) {
    return { key: `asset:${assetId}`, assetId: String(assetId), path: '' }
  }
  const panelPath = valueFrom(
    effectivePage.value,
    'screenshot_url',
    'thumbnail_url',
    'screenshot_path',
    'thumbnail_path',
    'image_path',
  )
  const path = panelPath || ((historicalMode.value || isTerminal.value) ? props.fallbackScreenshotPath : '')
  return path ? { key: `path:${path}`, assetId: '', path: String(path) } : null
}

function screenshotPath() {
  return screenshotSource()?.key || ''
}

async function loadFallbackImage() {
  const source = screenshotSource()
  const path = source?.key || ''
  const generation = ++fallbackRequestGeneration
  if (!path || !props.runId) {
    resetFallbackImage()
    return
  }
  if (
    fallbackImageUrl.value
    && fallbackImageSourcePath.value === path
  ) return
  resetFallbackImage()
  const runId = String(props.runId)
  try {
    let response
    if (source.assetId) response = await api.getAsset(source.assetId, 'blob')
    else if (source.path.startsWith('/api/assets/')) {
      const parsed = new URL(source.path, window.location.origin)
      response = await api.getAsset(decodeURIComponent(parsed.pathname.split('/').pop()), 'blob')
    } else if (source.path.startsWith('/api/inspections/')) {
      response = await api.getInspectionLiveAsset(source.path, 'blob')
    } else response = await api.getInspectionAsset(props.runId, source.path, 'blob')
    if (
      generation !== fallbackRequestGeneration
      || path !== screenshotPath()
      || runId !== String(props.runId)
    ) return
    const nextUrl = URL.createObjectURL(response.data)
    if (fallbackObjectUrl) URL.revokeObjectURL(fallbackObjectUrl)
    fallbackObjectUrl = nextUrl
    fallbackImageUrl.value = nextUrl
    fallbackImageSourcePath.value = path
    const image = new Image()
    image.onload = () => {
      if (generation !== fallbackRequestGeneration || fallbackImageUrl.value !== nextUrl) return
      fallbackImageNaturalSize.value = { width: image.naturalWidth, height: image.naturalHeight }
    }
    image.src = nextUrl
  } catch (error) {
    if (generation !== fallbackRequestGeneration) return
    resetFallbackImage()
    lastError.value = error.response?.data?.detail || error.message || '加载脱敏截图失败'
  }
}

function resetFallbackImage() {
  if (fallbackObjectUrl) URL.revokeObjectURL(fallbackObjectUrl)
  fallbackObjectUrl = ''
  fallbackImageUrl.value = ''
  fallbackImageSourcePath.value = ''
  fallbackImageNaturalSize.value = { width: 0, height: 0 }
}

function clearFallbackImage() {
  fallbackRequestGeneration++
  resetFallbackImage()
}

function handleVideoError(reason) {
  videoFailed.value = true
  videoStreamUrl.value = ''
  lastError.value = reason === 'decoder_fallback_requires_new_session' ? '视频解码器已切换，正在申请新会话' : '实时视频不可用，已降级到巡检截图'
  loadFallbackImage()
  scheduleSessionRetry()
}

function handleVideoDisconnected() {
  if (shouldStream.value && videoStreamUrl.value) handleVideoError('disconnected')
}

function selectAction(action) {
  selectedActionId.value = action?.id || ''
  emit('action-select', action)
}

async function start() {
  if (!props.active) return
  lastError.value = ''
  terminalEmitted = false
  retryCount = 0
  if (historicalMode.value) await loadActionMap()
  else if (shouldStream.value) await startLiveSession()
  else {
    await refreshSnapshot(true)
    await loadFallbackImage()
  }
}

function stop() {
  stopLiveSession()
}

watch(() => [props.runId, props.stateId], async () => {
  stopLiveSession()
  snapshot.value = {}
  historicalMap.value = null
  localEvents.value = []
  selectedActionId.value = ''
  terminalEmitted = false
  lastEmittedStateId = null
  clearFallbackImage()
  await start()
})

watch(() => props.active, async active => {
  if (active) await start()
  else stopLiveSession()
})

watch(() => props.runStatus, async status => {
  if (!TERMINAL_STATUSES.has(String(status || '').toUpperCase())) return
  stopLiveSession()
  await refreshSnapshot(true)
  await loadFallbackImage()
})

watch(screenshotPath, () => {
  clearFallbackImage()
  if (historicalMode.value || isTerminal.value || videoFailed.value) loadFallbackImage()
})

onMounted(start)

onBeforeUnmount(() => {
  stopLiveSession()
  clearFallbackImage()
})

defineExpose({ start, stop, refresh: refreshSnapshot, selectAction })
</script>

<template>
  <section class="inspection-live-panel" v-loading="loading">
    <div class="live-visual-column">
      <div class="live-summary">
        <div class="summary-heading">
          <div>
            <div class="summary-title">实时巡检</div>
            <div class="summary-subtitle">{{ stage }}</div>
          </div>
          <el-tag :type="eventConnected ? 'success' : isTerminal ? 'info' : 'warning'" effect="plain">
            {{ eventConnected ? '实时' : isTerminal ? effectiveStatus : sessionState === 'connecting' ? '连接中' : '快照' }}
          </el-tag>
        </div>
        <dl class="summary-grid">
          <div><dt>当前页面</dt><dd :title="currentPageLabel">{{ currentPageLabel }}</dd></div>
          <div><dt>当前动作</dt><dd :title="currentActionLabel">{{ currentActionLabel }}</dd></div>
          <div><dt>动作进度</dt><dd>{{ progressText }}</dd></div>
          <div v-if="frontierCounts.pending !== null"><dt>待执行动作</dt><dd>{{ frontierCounts.pending }}</dd></div>
        </dl>
        <div v-if="lastError" class="live-warning">
          <el-icon><WarningFilled /></el-icon><span>{{ lastError }}</span>
        </div>
      </div>

      <div class="live-stage">
        <ScrcpyPlayer
          v-if="shouldStream && videoStreamUrl && !videoFailed"
          :key="videoStreamUrl"
          :serial="deviceSerial"
          :stream-url="videoStreamUrl"
          :touch-enabled="false"
          :read-only="true"
          :overlays="playerOverlays"
          :source-width="sourceWidth"
          :source-height="sourceHeight"
          :selected-overlay-id="selectedActionId"
          @overlay-select="selectAction"
          @error="handleVideoError"
          @disconnected="handleVideoDisconnected"
        />

        <div v-else-if="fallbackImageReady" class="static-stage" aria-label="巡检脱敏截图回看">
          <img :src="fallbackImageUrl" alt="巡检状态截图" />
          <svg v-if="staticOverlayReady" class="static-stage__overlays" :viewBox="staticViewBox" preserveAspectRatio="xMidYMid meet">
            <g
              v-for="action in playerOverlays"
              :key="action.id"
              class="static-action"
              :class="[
                `is-${action.status.toLowerCase()}`,
                { 'is-coordinate': action.coordinate_only, 'is-selected': action.id === selectedActionId },
              ]"
              @click.stop="selectAction(action)"
            >
              <rect
                v-if="action.bounds.every(Number.isFinite)"
                :x="Math.min(action.x1, action.x2)"
                :y="Math.min(action.y1, action.y2)"
                :width="Math.abs(action.x2 - action.x1)"
                :height="Math.abs(action.y2 - action.y1)"
                rx="5"
                vector-effect="non-scaling-stroke"
              />
              <template v-if="action.display_order != null && action.bounds.every(Number.isFinite)">
                <circle :cx="Math.min(action.x1, action.x2) + 18" :cy="Math.min(action.y1, action.y2) + 18" r="16" />
                <text
                  :x="Math.min(action.x1, action.x2) + 18"
                  :y="Math.min(action.y1, action.y2) + 19"
                  class="static-action__number"
                >{{ action.display_order }}</text>
              </template>
              <text
                v-if="actionArrow(action) && action.bounds.every(Number.isFinite)"
                :x="(action.x1 + action.x2) / 2"
                :y="(action.y1 + action.y2) / 2"
                class="static-action__arrow"
              >{{ actionArrow(action) }}</text>
            </g>
          </svg>
        </div>

        <el-empty v-else :image-size="72" description="等待实时画面或脱敏状态截图">
          <el-button v-if="props.active && !isTerminal" :icon="Refresh" @click="start">重新连接观察台</el-button>
        </el-empty>
      </div>
    </div>

    <section class="actions-panel" aria-label="页面动作">
      <div class="actions-header">
        <div class="panel-title"><el-icon><Aim /></el-icon>页面动作</div>
        <el-segmented
          v-model="actionFilter"
          size="small"
          :options="[
            { label: '全部', value: 'all' },
            { label: '待执行', value: 'pending' },
            { label: '当前', value: 'current' },
          ]"
        />
      </div>

      <div class="action-list">
        <button
          v-for="action in filteredActions"
          :key="action.id"
          type="button"
          class="action-row"
          :class="{ 'is-selected': action.id === selectedActionId }"
          @click="selectAction(action)"
        >
          <span v-if="action.display_order != null" class="action-order">{{ action.display_order }}</span>
          <el-icon v-else class="action-icon"><component :is="actionIcon(action)" /></el-icon>
          <span class="action-main">
            <span class="action-label">{{ action.label }}</span>
            <span class="action-meta">
              {{ action.action_type }}<template v-if="action.locator_type"> · {{ action.locator_type }}</template>
              <template v-if="action.global_sequence != null"> · #{{ action.global_sequence }}</template>
            </span>
            <span v-if="action.reason" class="action-reason">{{ action.reason }}</span>
            <span v-if="['FAMILY_REUSED', 'CONTRACT_REUSED', 'NAVIGATION_REUSED', 'SAMPLED_OUT'].includes(action.execution_disposition) || action.coverage_source_transition_id" class="action-coverage">
              {{ inspectionExecutionDispositionLabel(action.execution_disposition || 'CONTRACT_REUSED') }}
            </span>
          </span>
          <el-tag :type="statusMeta(action.status).type" size="small" effect="plain">
            {{ statusMeta(action.status).label }}
          </el-tag>
        </button>
        <el-empty v-if="!filteredActions.length" :image-size="48" description="当前没有匹配动作" />
      </div>
    </section>

    <section class="events-panel" aria-label="最近事件">
      <div class="events-header">
        <div class="panel-title"><el-icon><Clock /></el-icon>最近事件</div>
      </div>
      <div class="event-list">
        <div v-for="(event, index) in eventItems" :key="`${eventTime(event)}-${index}`" class="event-row">
          <span class="event-dot"></span>
          <span class="event-text">{{ eventText(event) }}</span>
          <time>{{ eventTime(event) }}</time>
        </div>
        <div v-if="!eventItems.length" class="event-empty">等待巡检事件</div>
      </div>
    </section>
  </section>
</template>

<style scoped>
.inspection-live-panel {
  display: grid;
  grid-template-columns: minmax(210px, 0.78fr) minmax(240px, 1.02fr) minmax(210px, 0.86fr);
  height: 100%;
  min-height: 0;
  overflow: hidden;
  border: 1px solid #dcdfe6;
  border-radius: 6px;
  background: #fff;
}

.live-visual-column,
.actions-panel,
.events-panel {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}

.live-visual-column {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  background: #16181d;
}

.live-stage {
  min-width: 0;
  min-height: 0;
  display: flex;
  align-items: stretch;
  justify-content: center;
  overflow: hidden;
  background: #16181d;
}

.live-stage :deep(.scrcpy-player),
.live-stage :deep(.el-empty) {
  width: 100%;
  height: 100%;
}

.live-stage :deep(.player-content) {
  min-height: 0;
}

.static-stage {
  display: grid;
  width: 100%;
  height: 100%;
  min-height: 0;
  place-items: center;
  overflow: hidden;
}

.static-stage img,
.static-stage__overlays {
  grid-area: 1 / 1;
  width: 100%;
  height: 100%;
}

.static-stage img {
  object-fit: contain;
}

.static-stage__overlays {
  z-index: 1;
}

.static-action {
  color: #67c23a;
  cursor: pointer;
}

.static-action rect {
  fill: color-mix(in srgb, currentColor 20%, transparent);
  stroke: currentColor;
  stroke-width: 2;
}

.static-action circle {
  fill: currentColor;
  stroke: #fff;
  stroke-width: 1.5;
}

.static-action text {
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

.static-action__number { font-size: 18px; }
.static-action__arrow { font-size: 42px; }

.static-action.is-blocked,
.static-action.is-action_error,
.static-action.is-error { color: #f56c6c; }
.static-action.is-running,
.static-action.is-started,
.static-action.is-active,
.static-action.is-locator_drift,
.static-action.is-ambiguous,
.static-action.is-locator_ambiguous,
.static-action.is-locator_not_found,
.static-action.is-coordinate_unsafe,
.static-action.is-coordinate_stale,
.static-action.is-parent_recovery_failed,
.static-action.is-path_diverged { color: #e6a23c; }
.static-action.is-invoked,
.static-action.is-pass,
.static-action.is-self_loop,
.static-action.is-no_effect,
.static-action.is-skipped,
.static-action.is-unstable_parent,
.static-action.is-not_reached { color: #909399; }
.static-action.is-covered_by_family,
.static-action.is-covered_by_contract,
.static-action.is-sampled_out,
.static-action.is-navigation_reused,
.static-action.is-no_new_coverage,
.static-action.is-visual_stale,
.static-action.is-cancelled,
.static-action.is-budget_not_reached,
.static-action.is-filtered_non_actionable,
.static-action.is-queue_truncated,
.static-action.is-cycle_converged { color: #909399; }
.static-action.is-coordinate { color: #409eff; }
.static-action.is-no_effect.is-coordinate { color: #909399; }
.static-action.is-blocked rect,
.static-action.is-locator_drift rect,
.static-action.is-ambiguous rect,
.static-action.is-locator_ambiguous rect,
.static-action.is-locator_not_found rect,
.static-action.is-coordinate_unsafe rect,
.static-action.is-coordinate_stale rect,
.static-action.is-parent_recovery_failed rect,
.static-action.is-path_diverged rect,
.static-action.is-skipped rect,
.static-action.is-unstable_parent rect,
.static-action.is-not_reached rect,
.static-action.is-covered_by_family rect,
.static-action.is-covered_by_contract rect,
.static-action.is-sampled_out rect,
.static-action.is-navigation_reused rect,
.static-action.is-no_new_coverage rect,
.static-action.is-visual_stale rect,
.static-action.is-cancelled rect,
.static-action.is-budget_not_reached rect,
.static-action.is-filtered_non_actionable rect,
.static-action.is-queue_truncated rect,
.static-action.is-cycle_converged rect,
.static-action.is-coordinate rect { stroke-dasharray: 8 5; }
.static-action.is-selected rect { stroke: #409eff; stroke-width: 4; }

.actions-panel,
.events-panel {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  border-left: 1px solid #e4e7ed;
  background: #fff;
}

.live-summary {
  padding: 14px 16px 12px;
  border-bottom: 1px solid #ebeef5;
  background: #fff;
}

.summary-heading,
.actions-header,
.panel-title {
  display: flex;
  align-items: center;
}

.summary-heading {
  justify-content: space-between;
  gap: 12px;
}

.summary-title {
  color: #303133;
  font-size: 16px;
  font-weight: 600;
}

.summary-subtitle {
  max-width: 290px;
  margin-top: 3px;
  overflow: hidden;
  color: #909399;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.summary-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px 16px;
  margin: 12px 0 0;
}

.summary-grid div { min-width: 0; }
.summary-grid dt { color: #909399; font-size: 11px; }
.summary-grid dd {
  margin: 2px 0 0;
  overflow: hidden;
  color: #303133;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.live-warning {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin-top: 10px;
  padding: 7px 8px;
  border-left: 3px solid #e6a23c;
  background: #fdf6ec;
  color: #b88230;
  font-size: 12px;
}

.actions-header {
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
  border-bottom: 1px solid #ebeef5;
}

.events-header {
  padding: 12px;
  border-bottom: 1px solid #ebeef5;
}

.panel-title {
  gap: 6px;
  color: #303133;
  font-size: 13px;
  font-weight: 600;
}

.action-list {
  overflow: auto;
  padding: 6px;
}

.action-row {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px;
  border: 1px solid transparent;
  border-radius: 5px;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
}

.action-row:hover { background: #f5f7fa; }
.action-row.is-selected { border-color: #409eff; background: #ecf5ff; }

.action-order {
  display: inline-grid;
  width: 24px;
  height: 24px;
  place-items: center;
  border-radius: 50%;
  background: #67c23a;
  color: #fff;
  font-size: 12px;
  font-weight: 700;
}

.action-icon { width: 24px; color: #909399; }
.action-main { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.action-label { overflow: hidden; color: #303133; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.action-meta { color: #909399; font-size: 10px; }
.action-reason { color: #f56c6c; font-size: 10px; line-height: 1.35; }
.action-coverage { color: #409eff; font-size: 10px; line-height: 1.35; }

.events-panel {
  min-height: 0;
}

.event-list { min-height: 0; overflow: auto; padding: 6px 12px 12px; }
.event-row {
  display: grid;
  grid-template-columns: 8px minmax(0, 1fr) auto;
  align-items: center;
  gap: 6px;
  min-height: 24px;
  color: #606266;
  font-size: 11px;
}
.event-dot { width: 6px; height: 6px; border-radius: 50%; background: #409eff; }
.event-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.event-row time { color: #a8abb2; font-variant-numeric: tabular-nums; }
.event-empty { padding: 12px 0; color: #a8abb2; font-size: 12px; text-align: center; }

@media (max-width: 899px) {
  .inspection-live-panel {
    grid-template-columns: minmax(280px, 0.9fr) minmax(320px, 1.1fr);
    grid-template-rows: minmax(520px, 62vh) 240px;
    height: auto;
    min-height: 0;
    overflow: visible;
  }
  .events-panel {
    grid-column: 1 / -1;
    min-height: 240px;
    border-top: 1px solid #e4e7ed;
    border-left: 0;
  }
}

@media (max-width: 640px) {
  .inspection-live-panel {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: minmax(480px, 62vh) minmax(420px, auto) minmax(240px, auto);
  }
  .actions-panel,
  .events-panel {
    grid-column: 1;
    border-top: 1px solid #e4e7ed;
    border-left: 0;
  }
  .actions-panel { min-height: 420px; }
  .events-panel { min-height: 240px; }
  .actions-header { align-items: flex-start; flex-direction: column; }
  .actions-header :deep(.el-segmented) { width: 100%; }
  .summary-grid { grid-template-columns: 1fr; }
}
</style>
