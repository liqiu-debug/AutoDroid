<script setup>
import { ref, computed, nextTick, onMounted, onUnmounted, watch } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { useCaseStore } from '@/stores/useCaseStore'
import { ElMessage } from 'element-plus'
import api from '@/api'
import { findBestNodeAtPoint, findBestRecordableNode } from '@/utils/recordableNode'
import ScrcpyPlayer from './ScrcpyPlayer.vue'

const caseStore = useCaseStore()

// Device state
const screenshot = ref('')
const hierarchyXml = ref('')
const deviceInfo = ref(null)
const nodes = ref([])
const loading = ref(false)
const liveHierarchyLoading = ref(false)
const syncMode = ref(false)

// 实时投屏模式
const liveMode = ref(false)
const connectedDevices = ref([])
const streamDevices = ref([])
const selectedSerial = ref('')
const liveNodes = ref([])  // 投屏模式下的 UI 层级节点
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
const previewMode = computed({
  get: () => (liveMode.value ? 'live' : 'static'),
  set: (value) => {
    const nextLiveMode = value === 'live'
    if (!nextLiveMode && isSelectedDeviceBusy.value) {
      ElMessage.warning('当前设备执行中，仅支持实时只读观察')
      liveMode.value = true
      return
    }
    liveMode.value = nextLiveMode
  }
})
const interactionMode = computed({
  get: () => (syncMode.value ? 'record' : 'explore'),
  set: (value) => {
    syncMode.value = value === 'record'
  }
})
const selectedStreamDevice = computed(() =>
  streamDevices.value.find(d => d.serial === selectedSerial.value) || null
)
const isSelectedStreamReady = computed(() => Boolean(selectedStreamDevice.value?.ready))
const selectedStreamError = computed(() => String(selectedStreamDevice.value?.error || '').trim())
const hierarchyHash = ref('')
const liveHierarchyStatus = ref('idle')
const liveHierarchyError = ref('')
const liveHierarchyLastUpdatedAt = ref(0)



// Hover state
const hoveredNode = ref(null)
const canvasRef = ref(null)
const imgRef = ref(null)
const livePromptHostRef = ref(null)
const ocrCropMode = ref(false)
const ocrCropStep = ref(null)
const ocrCropDragging = ref(false)
const ocrCropStart = ref(null)
const ocrCropCurrent = ref(null)
const ocrCropJustCompleted = ref(false)  // 标记框选刚完成
const ocrCropCompletionTimestamp = ref(0)  // 框选完成的时间戳

// 图像模板截取框选模式
const imageCropMode = ref(false)
const imageCropStep = ref(null)
const quickImagePrompt = ref(null)
const quickImageCapture = ref(null)
const pendingQuickImageDraft = ref(null)
const skipNextStaticDump = ref(false)

const QUICK_IMAGE_PROMPT_WIDTH = 248
const QUICK_IMAGE_HINT_KEYWORDS = ['无 Desc/Text', '图像点击', '未识别到可录制元素']
const IOS_LIVE_PREVIEW_POLL_MS = 900
const ANDROID_LIVE_PREVIEW_POLL_IDLE_MS = 700
const ANDROID_LIVE_PREVIEW_POLL_ACTIVE_MS = 300
const LIVE_PREVIEW_BUSY_POLL_MS = 1800
const LIVE_PREVIEW_POLL_ACTIVE_WINDOW_MS = 2500

const getErrorDetail = (err, fallback = '操作失败') => {
  return err?.response?.data?.detail || err?.message || fallback
}

const canObserveDeviceInCurrentMode = (device) => {
  const status = String(device?.status || '').trim().toUpperCase()
  if (!status) return false
  if (liveMode.value) {
    return status !== 'OFFLINE' && status !== 'WDA_DOWN'
  }
  return status === 'IDLE'
}

const isQuickImageSuggestedError = (err) => {
  const detail = getErrorDetail(err, '')
  return QUICK_IMAGE_HINT_KEYWORDS.some(keyword => detail.includes(keyword))
}

const resetCropSelection = () => {
  ocrCropDragging.value = false
  ocrCropStart.value = null
  ocrCropCurrent.value = null
}

const clearQuickImagePrompt = () => {
  quickImagePrompt.value = null
}

const isSameStepTarget = (currentStep, nextStep) => {
  if (!currentStep || !nextStep) return false
  const currentUuid = String(currentStep.uuid || '').trim()
  const nextUuid = String(nextStep.uuid || '').trim()
  if (currentUuid && nextUuid) {
    return currentUuid === nextUuid
  }
  return currentStep === nextStep
}

const stopImageCrop = ({ notify = false } = {}) => {
  quickImageCapture.value = null
  imageCropMode.value = false
  imageCropStep.value = null
  resetCropSelection()
  if (notify) {
    ElMessage.info('已退出图像截取模式')
  }
}

const cancelQuickImageCapture = () => {
  stopImageCrop()
}

const getScreenshotBase64 = () => screenshot.value.replace(/^data:image\/\w+;base64,/, '')

const getFallbackNodeAt = (nodeList, realX, realY) => {
  return findBestNodeAtPoint(nodeList, realX, realY)
}

const buildCanvasCropCoord = (realX, realY) => {
  if (!imgRef.value || !canvasRef.value) return null

  const rect = imgRef.value.getBoundingClientRect()
  const scaleX = imgRef.value.naturalWidth / rect.width
  const scaleY = imgRef.value.naturalHeight / rect.height

  return {
    canvasX: realX / scaleX,
    canvasY: realY / scaleY,
    realX: Math.round(realX),
    realY: Math.round(realY),
    scaleX,
    scaleY,
    rect
  }
}

const expandBoundsForImageCrop = (node) => {
  const width = Math.max(1, Number(node?.x2 || 0) - Number(node?.x1 || 0))
  const height = Math.max(1, Number(node?.y2 || 0) - Number(node?.y1 || 0))
  const minSize = 44
  const padX = Math.max(12, Math.round(width * 0.35))
  const padY = Math.max(12, Math.round(height * 0.35))

  let x1 = Number(node?.x1 || 0) - padX
  let y1 = Number(node?.y1 || 0) - padY
  let x2 = Number(node?.x2 || 0) + padX
  let y2 = Number(node?.y2 || 0) + padY

  if (x2 - x1 < minSize) {
    const extra = (minSize - (x2 - x1)) / 2
    x1 -= extra
    x2 += extra
  }
  if (y2 - y1 < minSize) {
    const extra = (minSize - (y2 - y1)) / 2
    y1 -= extra
    y2 += extra
  }

  if (imgRef.value?.naturalWidth) {
    x1 = Math.max(0, x1)
    x2 = Math.min(imgRef.value.naturalWidth, x2)
  }
  if (imgRef.value?.naturalHeight) {
    y1 = Math.max(0, y1)
    y2 = Math.min(imgRef.value.naturalHeight, y2)
  }

  return {
    x1: Math.round(x1),
    y1: Math.round(y1),
    x2: Math.round(x2),
    y2: Math.round(y2)
  }
}

const applyCropBoundsToOverlay = (bounds) => {
  const start = buildCanvasCropCoord(bounds.x1, bounds.y1)
  const end = buildCanvasCropCoord(bounds.x2, bounds.y2)
  if (!start || !end) return
  ocrCropStart.value = start
  ocrCropCurrent.value = end
}

const getCurrentCropSelection = () => {
  if (!ocrCropStart.value || !ocrCropCurrent.value) return null

  return {
    x1: Math.round(Math.min(ocrCropStart.value.realX, ocrCropCurrent.value.realX)),
    y1: Math.round(Math.min(ocrCropStart.value.realY, ocrCropCurrent.value.realY)),
    x2: Math.round(Math.max(ocrCropStart.value.realX, ocrCropCurrent.value.realX)),
    y2: Math.round(Math.max(ocrCropStart.value.realY, ocrCropCurrent.value.realY))
  }
}

const isSameSelection = (left, right) => {
  if (!left || !right) return false
  return ['x1', 'y1', 'x2', 'y2'].every((key) => Number(left[key]) === Number(right[key]))
}

const getSelectionCenterPoint = (selection) => {
  if (!selection) return null
  return {
    x: Math.round((Number(selection.x1 || 0) + Number(selection.x2 || 0)) / 2),
    y: Math.round((Number(selection.y1 || 0) + Number(selection.y2 || 0)) / 2)
  }
}

const buildQuickImageStepDescription = (node) => {
  const resourceId = String(node?.resourceId || '').trim()
  if (resourceId) {
    return `图像点击 [${resourceId.split('/').pop()}]`
  }

  const className = String(node?.className || '').trim()
  const classTail = className ? className.split('.').pop() : '目标区域'
  return `图像点击 [${classTail}]`
}

const addQuickImageStep = (imagePath, node) => {
  caseStore.addStep({
    action: 'click_image',
    selector: imagePath,
    selector_type: 'image',
    value: '',
    description: buildQuickImageStepDescription(node),
    error_strategy: 'ABORT'
  })
}

const cropTemplateFromBase64 = async (screenshotBase64, selection) => {
  const res = await api.cropTemplate(
    screenshotBase64,
    selection.x1,
    selection.y1,
    selection.x2,
    selection.y2
  )
  return res.data.image_path
}

const executeRawTapAndRefresh = async (x, y) => {
  const res = await api.interactDevice(x, y, 'click', hierarchyXml.value, null, selectedSerial.value, false)
  if (res?.data?.dump) {
    updateStateFromDump(res.data.dump)
  }
  return res
}

const finalizeQuickImageStep = async ({ screenshotBase64, selection, node, tapX, tapY, executeTap }) => {
  loading.value = true
  let success = false
  try {
    const imagePath = await cropTemplateFromBase64(screenshotBase64, selection)
    addQuickImageStep(imagePath, node)

    let tapError = ''
    if (executeTap) {
      try {
        await executeRawTapAndRefresh(tapX, tapY)
      } catch (err) {
        tapError = getErrorDetail(err, '实际点击失败')
      }
    }

    if (tapError) {
      ElMessage.warning(`图像点击步骤已添加，但实际点击失败：${tapError}`)
    } else {
      ElMessage.success(executeTap ? '已转为图像点击并完成实际点击' : '已添加图像点击步骤')
    }
    success = true
  } catch (err) {
    ElMessage.error(`图像点击录制失败：${getErrorDetail(err, '操作失败')}`)
  } finally {
    loading.value = false
  }
  return success
}

const parseNumberAttr = (value) => {
  const parsed = Number.parseFloat(String(value ?? '').trim())
  return Number.isFinite(parsed) ? Math.round(parsed) : null
}

const resolveHierarchyScale = (deviceInfoPayload) => {
  const platform = String(deviceInfoPayload?.platform || '').trim().toLowerCase()
  if (platform !== 'ios') return 1
  const scale = Number.parseFloat(String(deviceInfoPayload?.scale ?? '1').trim())
  return Number.isFinite(scale) && scale > 0 ? scale : 1
}

const applyBoundsScale = (bounds, scale = 1) => {
  const safeScale = Number.isFinite(scale) && scale > 0 ? scale : 1
  if (safeScale === 1 || !bounds) return bounds
  return {
    x1: Math.round(bounds.x1 * safeScale),
    y1: Math.round(bounds.y1 * safeScale),
    x2: Math.round(bounds.x2 * safeScale),
    y2: Math.round(bounds.y2 * safeScale)
  }
}

const parseNodeBounds = (node, scale = 1) => {
  const bounds = node.getAttribute('bounds')
  if (bounds) {
    const match = bounds.match(/\[(\d+),(\d+)\]\[(\d+),(\d+)\]/)
    if (match) {
      return applyBoundsScale({
        x1: Number.parseInt(match[1], 10),
        y1: Number.parseInt(match[2], 10),
        x2: Number.parseInt(match[3], 10),
        y2: Number.parseInt(match[4], 10)
      }, scale)
    }
  }

  const left = parseNumberAttr(node.getAttribute('left'))
  const top = parseNumberAttr(node.getAttribute('top'))
  const right = parseNumberAttr(node.getAttribute('right'))
  const bottom = parseNumberAttr(node.getAttribute('bottom'))
  if ([left, top, right, bottom].every(value => value !== null)) {
    return applyBoundsScale({ x1: left, y1: top, x2: right, y2: bottom }, scale)
  }

  const x = parseNumberAttr(node.getAttribute('x'))
  const y = parseNumberAttr(node.getAttribute('y'))
  const width = parseNumberAttr(node.getAttribute('width'))
  const height = parseNumberAttr(node.getAttribute('height'))
  if ([x, y, width, height].every(value => value !== null)) {
    return applyBoundsScale({
      x1: x,
      y1: y,
      x2: x + Math.max(width, 0),
      y2: y + Math.max(height, 0)
    }, scale)
  }

  if ([x, y, right, bottom].every(value => value !== null)) {
    return applyBoundsScale({ x1: x, y1: y, x2: right, y2: bottom }, scale)
  }

  return null
}

const shouldSkipCanvasNode = (className, depth) => {
  const normalizedClassName = String(className || '').trim()
  if (!normalizedClassName) return false
  if (normalizedClassName === 'AppiumAUT' || normalizedClassName === 'hierarchy') return true
  if (normalizedClassName.includes('XCUIElementTypeApplication')) return true
  if (normalizedClassName.includes('XCUIElementTypeWindow') && depth <= 2) return true
  return false
}

// Parse XML hierarchy into node list with bounds
const parseHierarchy = (xml, deviceInfoPayload = null) => {
  if (!xml) return []
  const parser = new DOMParser()
  const doc = parser.parseFromString(xml, 'text/xml')
  const result = []
  const coordinateScale = resolveHierarchyScale(deviceInfoPayload)
  
  const traverse = (node, depth = 0) => {
    if (node.nodeType === 1) {
      const bounds = parseNodeBounds(node, coordinateScale)
      if (bounds) {
        const { x1, y1, x2, y2 } = bounds
        const hasElementChildren = Array.from(node.childNodes || []).some(child => child.nodeType === 1)
        const className = node.getAttribute('class') || node.getAttribute('type') || node.nodeName || ''
        if (!shouldSkipCanvasNode(className, depth)) {
          result.push({
            x1,
            y1,
            x2,
            y2,
            text: node.getAttribute('text') || node.getAttribute('label') || node.getAttribute('value') || '',
            resourceId: node.getAttribute('resource-id') || node.getAttribute('resourceId') || node.getAttribute('id') || '',
            className,
            contentDesc: node.getAttribute('content-desc') || node.getAttribute('name') || '',
            depth,
            childCount: Array.from(node.childNodes || []).filter(child => child.nodeType === 1).length,
            isLeaf: !hasElementChildren,
            area: Math.max(0, x2 - x1) * Math.max(0, y2 - y1)
          })
        }
      }
    }
    for (const child of node.childNodes) {
      traverse(child, depth + 1)
    }
  }
  traverse(doc.documentElement)
  return result
}

const getCurrentNodeList = () => (liveMode.value ? liveNodes.value : nodes.value)

// Get mapped coordinates
const getMappedCoordinates = (clientX, clientY) => {
  if (!imgRef.value || !canvasRef.value) return null
  
  const rect = imgRef.value.getBoundingClientRect()
  const x = clientX - rect.left
  const y = clientY - rect.top
  
  const scaleX = imgRef.value.naturalWidth / rect.width
  const scaleY = imgRef.value.naturalHeight / rect.height

  // Check if click is within image bounds
  if (x < 0 || x > rect.width || y < 0 || y > rect.height) {
    return null
  }
  
  return {
    canvasX: x,
    canvasY: y,
    realX: Math.round(x * scaleX),
    realY: Math.round(y * scaleY),
    scaleX,
    scaleY,
    rect
  }
}

// Find node at coordinates
const findNodeAt = (realX, realY) => {
  return findBestRecordableNode(getCurrentNodeList(), realX, realY)
}

// 判断是否处于任何框选模式
const isAnyCropMode = computed(() => ocrCropMode.value || imageCropMode.value)
const activeImageCropStepUuid = computed(() => {
  if (!imageCropMode.value || quickImageCapture.value) return ''
  return String(imageCropStep.value?.uuid || '').trim()
})

// Mouse move handler
const onCanvasMouseMove = (event) => {
  // 框选模式下优先处理框选逻辑，不进行元素高亮
  if (isAnyCropMode.value) {
    if (ocrCropDragging.value) {
      const coords = getMappedCoordinates(event.clientX, event.clientY)
      if (coords) {
        ocrCropCurrent.value = coords
      }
    }
    hoveredNode.value = null  // 禁用元素高亮
    return
  }

  const coords = getMappedCoordinates(event.clientX, event.clientY)
  if (!coords) {
    hoveredNode.value = null
    return
  }
  hoveredNode.value = findNodeAt(coords.realX, coords.realY)
}

const onCanvasMouseLeave = () => {
  hoveredNode.value = null
}

const onCanvasMouseDown = (event) => {
  if (!isAnyCropMode.value) return
  
  // 清除之前的框选完成标志
  ocrCropJustCompleted.value = false
  
  const coords = getMappedCoordinates(event.clientX, event.clientY)
  if (!coords) return
  ocrCropDragging.value = true
  ocrCropStart.value = coords
  ocrCropCurrent.value = coords
  event.preventDefault()
  
  // 阻止 click 事件的默认行为
  event.stopPropagation()
}

const onCanvasMouseUp = async (event) => {
  if (!isAnyCropMode.value || !ocrCropDragging.value || !imgRef.value) return

  const coords = getMappedCoordinates(event.clientX, event.clientY)
  const end = coords || ocrCropCurrent.value
  const start = ocrCropStart.value
  ocrCropDragging.value = false

  if (!start || !end) return

  const x1 = Math.min(start.realX, end.realX)
  const y1 = Math.min(start.realY, end.realY)
  const x2 = Math.max(start.realX, end.realX)
  const y2 = Math.max(start.realY, end.realY)

  if (x2 - x1 < 4 || y2 - y1 < 4) {
    ElMessage.warning('框选区域过小，请重新框选')
    resetCropSelection()
    return
  }

  if (imageCropMode.value && quickImageCapture.value) {
    ocrCropStart.value = start
    ocrCropCurrent.value = end
    if (event) {
      event.preventDefault()
      event.stopPropagation()
    }
    return
  }

  // 图像模板截取模式
  if (imageCropMode.value && imageCropStep.value) {
    try {
      // 从当前截图获取 base64（去除 data:image/png;base64, 前缀）
      const b64 = screenshot.value.replace(/^data:image\/\w+;base64,/, '')
      const res = await api.cropTemplate(b64, x1, y1, x2, y2)
      const imagePath = res.data.image_path

      // 回填步骤
      imageCropStep.value.selector = imagePath
      imageCropStep.value.selector_type = 'image'
      if (!imageCropStep.value.platform_overrides || typeof imageCropStep.value.platform_overrides !== 'object') {
        imageCropStep.value.platform_overrides = {}
      }
      imageCropStep.value.platform_overrides.android = {
        selector: imagePath,
        by: 'image'
      }
      ElMessage.success('图像模板已截取并保存')
    } catch (err) {
      console.error('截取图像模板失败:', err)
      ElMessage.error('截取图像模板失败: ' + (err.response?.data?.detail || err.message))
    }

    imageCropMode.value = false
    imageCropStep.value = null
    resetCropSelection()
  }

  // OCR 框选模式
  if (ocrCropMode.value && ocrCropStep.value) {
    const imgW = imgRef.value.naturalWidth || 1
    const imgH = imgRef.value.naturalHeight || 1
    const region = [
      Number((x1 / imgW).toFixed(4)),
      Number((y1 / imgH).toFixed(4)),
      Number((x2 / imgW).toFixed(4)),
      Number((y2 / imgH).toFixed(4))
    ]

    const regionText = `[${region.join(', ')}]`
    ocrCropStep.value.selector = regionText
    if (!ocrCropStep.value.platform_overrides || typeof ocrCropStep.value.platform_overrides !== 'object') {
      ocrCropStep.value.platform_overrides = {}
    }
    ocrCropStep.value.platform_overrides.android = {
      selector: regionText,
      by: 'text'
    }
    ElMessage.success('OCR 截取区域已回填')

    ocrCropMode.value = false
    ocrCropStep.value = null
    resetCropSelection()
  }

  // 标记框选刚完成，防止触发点击录制
  ocrCropJustCompleted.value = true
  ocrCropCompletionTimestamp.value = Date.now()

  resetCropSelection()

  // 阻止后续的 click 事件
  if (event) {
    event.preventDefault()
    event.stopPropagation()
  }

  // 300ms 后重置标志
  setTimeout(() => {
    ocrCropJustCompleted.value = false
  }, 300)
}

const resolveQuickImagePromptPosition = (hostWidth, hostHeight, baseLeft, baseTop) => {
  const maxLeft = Math.max(16, hostWidth - QUICK_IMAGE_PROMPT_WIDTH - 16)
  const maxTop = Math.max(16, hostHeight - 116)
  return {
    left: Math.min(Math.max(16, baseLeft + 16), maxLeft),
    top: Math.min(Math.max(16, baseTop + 16), maxTop)
  }
}

const showQuickImagePrompt = ({ node, x, y, canvasX, canvasY, executeTap }) => {
  if (!canvasRef.value || !imgRef.value) return
  const canvasRect = canvasRef.value.getBoundingClientRect()
  const imgRect = imgRef.value.getBoundingClientRect()
  const baseLeft = imgRect.left - canvasRect.left + canvasX
  const baseTop = imgRect.top - canvasRect.top + canvasY
  const position = resolveQuickImagePromptPosition(
    canvasRef.value.clientWidth,
    canvasRef.value.clientHeight,
    baseLeft,
    baseTop
  )
  quickImagePrompt.value = {
    host: 'static',
    node,
    x,
    y,
    executeTap,
    ...position
  }
}

const showLiveQuickImagePrompt = ({ node, x, y, relX = 0.5, relY = 0.5, executeTap }) => {
  if (!livePromptHostRef.value) return
  const hostWidth = livePromptHostRef.value.clientWidth
  const hostHeight = livePromptHostRef.value.clientHeight
  const safeRelX = Math.min(1, Math.max(0, Number(relX) || 0.5))
  const safeRelY = Math.min(1, Math.max(0, Number(relY) || 0.5))
  const position = resolveQuickImagePromptPosition(
    hostWidth,
    hostHeight,
    hostWidth * safeRelX,
    hostHeight * safeRelY
  )
  quickImagePrompt.value = {
    host: 'live',
    node,
    x,
    y,
    executeTap,
    ...position
  }
}

const activateQuickImageCaptureDraft = (draft) => {
  if (!draft || !screenshot.value) return

  const initialSelection = expandBoundsForImageCrop(draft.node)
  clearQuickImagePrompt()
  imageCropMode.value = true
  imageCropStep.value = null
  quickImageCapture.value = {
    node: draft.node,
    x: draft.x,
    y: draft.y,
    executeTap: Boolean(draft.executeTap),
    initialSelection
  }
  resetCropSelection()
  applyCropBoundsToOverlay(initialSelection)
  ElMessage.info('已切换为图像点击，可直接确认或拖拽调整截取区域')
}

const beginQuickImageCapture = async (draft = quickImagePrompt.value) => {
  if (!draft) return

  if (draft.host === 'live') {
    if (!selectedSerial.value) {
      ElMessage.warning('请先选择一台调试设备')
      return
    }
    loading.value = true
    try {
      const res = await api.getDeviceDump(selectedSerial.value, {
        includeScreenshot: true,
        includeHierarchy: true,
        includeDeviceInfo: shouldRequestDeviceInfo()
      })
      updateStateFromDump(res.data)
      pendingQuickImageDraft.value = { ...draft, host: 'static' }
      skipNextStaticDump.value = true
      liveMode.value = false
      await nextTick()
      activateQuickImageCaptureDraft(pendingQuickImageDraft.value)
      pendingQuickImageDraft.value = null
    } catch (err) {
      ElMessage.error('切换图像点击失败: ' + getErrorDetail(err, '获取设备截图失败'))
    } finally {
      loading.value = false
    }
    return
  }

  activateQuickImageCaptureDraft(draft)
}

const confirmQuickImageCapture = async () => {
  if (!quickImageCapture.value) return

  const selection = getCurrentCropSelection()
  if (!selection) {
    ElMessage.warning('请先框选图像区域')
    return
  }

  const draft = quickImageCapture.value
  const tapPoint = isSameSelection(selection, draft.initialSelection)
    ? { x: draft.x, y: draft.y }
    : getSelectionCenterPoint(selection)
  const success = await finalizeQuickImageStep({
    screenshotBase64: getScreenshotBase64(),
    selection,
    node: draft.node,
    tapX: tapPoint?.x ?? draft.x,
    tapY: tapPoint?.y ?? draft.y,
    executeTap: draft.executeTap
  })
  if (success) {
    cancelQuickImageCapture()
  }
}

function ensureInteractionReady(actionLabel = '录制') {
  if (!selectedSerial.value) {
    ElMessage.warning('请先选择一台调试设备')
    return false
  }

  const device = selectedRecordingDevice.value
  if (!device) {
    ElMessage.warning('当前设备不存在，请刷新设备列表')
    return false
  }

  if (String(device.status || '').toUpperCase() === 'BUSY') {
    ElMessage.warning(`当前设备正在执行任务，暂不支持${actionLabel}，可继续观察实时投屏`)
    return false
  }

  if (device.status !== 'IDLE') {
    ElMessage.warning(`当前设备不可用于${actionLabel}（${statusLabel(device.status)}）`)
    return false
  }

  return true
}

// Click handler
const onCanvasClick = async (event) => {
  // 检查 OCR 框选模式或刚完成框选
  if (ocrCropMode.value || imageCropMode.value || ocrCropJustCompleted.value) {
    return
  }
  if (!ensureInteractionReady(syncMode.value ? '录制' : '探索')) return
  if (loading.value) return

  const coords = getMappedCoordinates(event.clientX, event.clientY)
  if (!coords) return
  clearQuickImagePrompt()

  if (!syncMode.value) {
    loading.value = true
    try {
      await executeRawTapAndRefresh(coords.realX, coords.realY)
      ElMessage.success('操作成功')
    } catch (err) {
      ElMessage.error(getErrorDetail(err, '交互失败'))
    } finally {
      loading.value = false
    }
    return
  }

  const currentNodes = getCurrentNodeList()
  const recordableNode = findBestRecordableNode(currentNodes, coords.realX, coords.realY)
  const fallbackNode = getFallbackNodeAt(currentNodes, coords.realX, coords.realY)

  if (!recordableNode && !fallbackNode) {
    return
  }

  if (!recordableNode && fallbackNode) {
    showQuickImagePrompt({
      node: fallbackNode,
      x: coords.realX,
      y: coords.realY,
      canvasX: coords.canvasX,
      canvasY: coords.canvasY,
      executeTap: Boolean(syncMode.value)
    })
    return
  }

  loading.value = true
  try {
    const res = await api.interactDevice(coords.realX, coords.realY, 'click', hierarchyXml.value, null, selectedSerial.value)

    // Update Device State
    updateStateFromDump(res.data.dump)

    // Add Step
    if (res.data.step) {
      caseStore.addStep(res.data.step)
      ElMessage.success('操作成功并添加步骤')
    }
  } catch (err) {
    if (fallbackNode && isQuickImageSuggestedError(err)) {
      showQuickImagePrompt({
        node: fallbackNode,
        x: coords.realX,
        y: coords.realY,
        canvasX: coords.canvasX,
        canvasY: coords.canvasY,
        executeTap: true
      })
    } else {
      console.error(err)
      ElMessage.error(getErrorDetail(err, '交互失败'))
    }
  } finally {
    loading.value = false
  }
}

const startOcrCrop = (step) => {
  if (!ensureInteractionReady('OCR 框选')) return
  if (liveMode.value && !isIosLivePreview.value) {
    ElMessage.warning('请切换到静态截图模式后再进行框选')
    return
  }
  if (!screenshot.value) {
    ElMessage.warning('当前无设备截图，请先刷新')
    return
  }
  clearQuickImagePrompt()
  cancelQuickImageCapture()
  ocrCropMode.value = true
  ocrCropStep.value = step
  resetCropSelection()
  ElMessage.info('请在截图上按住鼠标拖拽框选 OCR 区域')
}

const startImageCrop = (step) => {
  if (imageCropMode.value && imageCropStep.value && isSameStepTarget(imageCropStep.value, step)) {
    stopImageCrop({ notify: true })
    return
  }
  if (!ensureInteractionReady('图像截取')) return
  if (liveMode.value && !isIosLivePreview.value) {
    ElMessage.warning('请切换到静态截图模式后再进行框选')
    return
  }
  if (!screenshot.value) {
    ElMessage.warning('当前无设备截图，请先刷新')
    return
  }
  clearQuickImagePrompt()
  stopImageCrop()
  ocrCropMode.value = false
  ocrCropStep.value = null
  imageCropMode.value = true
  imageCropStep.value = step
  resetCropSelection()
  ElMessage.info('请在截图上按住鼠标拖拽框选目标元素区域')
}

const getCropOverlayStyle = () => {
  if (!isAnyCropMode.value || !ocrCropStart.value || !ocrCropCurrent.value || !imgRef.value || !canvasRef.value) {
    return { display: 'none' }
  }

  const imgRect = imgRef.value.getBoundingClientRect()
  const canvasRect = canvasRef.value.getBoundingClientRect()
  const offsetX = imgRect.left - canvasRect.left
  const offsetY = imgRect.top - canvasRect.top
  const x1 = Math.min(ocrCropStart.value.canvasX, ocrCropCurrent.value.canvasX)
  const y1 = Math.min(ocrCropStart.value.canvasY, ocrCropCurrent.value.canvasY)
  const x2 = Math.max(ocrCropStart.value.canvasX, ocrCropCurrent.value.canvasX)
  const y2 = Math.max(ocrCropStart.value.canvasY, ocrCropCurrent.value.canvasY)

  return {
    display: 'block',
    position: 'absolute',
    left: `${offsetX + x1}px`,
    top: `${offsetY + y1}px`,
    width: `${x2 - x1}px`,
    height: `${y2 - y1}px`,
    border: '2px dashed ' + (imageCropMode.value ? '#409eff' : '#67c23a'),
    backgroundColor: imageCropMode.value ? 'rgba(64, 158, 255, 0.18)' : 'rgba(103, 194, 58, 0.18)',
    pointerEvents: 'none',
    boxSizing: 'border-box',
    borderRadius: '4px'
  }
}

// Get overlay style for hovered node
const getOverlayStyle = () => {
  if (!hoveredNode.value || !imgRef.value) return { display: 'none' }
  
  const rect = imgRef.value.getBoundingClientRect()
  const canvasRect = canvasRef.value.getBoundingClientRect()
  
  const scaleX = rect.width / imgRef.value.naturalWidth
  const scaleY = rect.height / imgRef.value.naturalHeight
  
  const offsetX = rect.left - canvasRect.left
  const offsetY = rect.top - canvasRect.top
  
  const node = hoveredNode.value
  return {
    display: 'block',
    position: 'absolute',
    left: `${offsetX + node.x1 * scaleX}px`,
    top: `${offsetY + node.y1 * scaleY}px`,
    width: `${(node.x2 - node.x1) * scaleX}px`,
    height: `${(node.y2 - node.y1) * scaleY}px`,
    border: '2px solid #e74c3c',
    backgroundColor: 'rgba(231, 76, 60, 0.15)',
    pointerEvents: 'none',
    boxSizing: 'border-box',
    borderRadius: '4px'
  }
}

// Fetch device dump
const fetchDump = async () => {
  clearQuickImagePrompt()
  if (!selectedSerial.value) {
    screenshot.value = ''
    hierarchyXml.value = ''
    hierarchyHash.value = ''
    deviceInfo.value = null
    nodes.value = []
    liveNodes.value = []
    liveHierarchyStatus.value = 'idle'
    liveHierarchyError.value = ''
    liveHierarchyLastUpdatedAt.value = 0
    return
  }
  loading.value = true
  try {
    const res = await api.getDeviceDump(selectedSerial.value, {
      includeScreenshot: true,
      includeHierarchy: true,
      includeDeviceInfo: shouldRequestDeviceInfo()
    })
    updateStateFromDump(res.data)
  } catch (err) {
    ElMessage.error('获取设备状态失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    loading.value = false
  }
}



// 获取设备列表
const fetchDevices = async () => {
  try {
    const [deviceRes, streamRes] = await Promise.all([
      api.getDeviceList().catch(() => ({ data: [] })),
      api.getDevices().catch(() => ({ data: [] }))
    ])

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
  } catch (err) {
    console.error('获取设备列表失败:', err)
  }
}

let liveStreamPollTimer = null
let livePreviewPollTimer = null
let iosLivePreviewLightDumpCount = 0
let livePreviewBoostUntil = 0

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

  return {
    includeScreenshot: true,
    includeHierarchy,
    includeDeviceInfo: shouldRequestDeviceInfo()
  }
}

const startLiveStreamPolling = () => {
  if (liveStreamPollTimer) return
  liveStreamPollTimer = setInterval(() => {
    fetchDevices()
  }, 2000)
}

const stopLiveStreamPolling = () => {
  if (!liveStreamPollTimer) return
  clearInterval(liveStreamPollTimer)
  liveStreamPollTimer = null
}

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
  if (!shouldPollLivePreview.value || livePreviewPollTimer) return
  const delay = immediate ? 0 : getLivePreviewPollInterval()
  livePreviewPollTimer = setTimeout(async () => {
    livePreviewPollTimer = null
    if (!shouldPollLivePreview.value) return
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

const markLiveHierarchyFresh = () => {
  liveHierarchyStatus.value = 'fresh'
  liveHierarchyError.value = ''
  liveHierarchyLastUpdatedAt.value = Date.now()
}

const markLiveHierarchyStale = (errorMessage = '') => {
  liveHierarchyStatus.value = 'stale'
  liveHierarchyError.value = String(errorMessage || '').trim()
}

// 当选择设备变化时,根据当前模式刷新
const onDeviceChange = async () => {
  // 先刷新设备列表,获取最新状态
  clearQuickImagePrompt()
  cancelQuickImageCapture()
  hierarchyXml.value = ''
  hierarchyHash.value = ''
  nodes.value = []
  liveNodes.value = []
  liveHierarchyStatus.value = 'idle'
  liveHierarchyError.value = ''
  liveHierarchyLastUpdatedAt.value = 0
  await fetchDevices()

  if (!selectedSerial.value) {
    livePreviewBoostUntil = 0
    hierarchyXml.value = ''
    hierarchyHash.value = ''
    liveNodes.value = []
    return
  }
  
  // 根据模式刷新设备内容
  if (liveMode.value) {
    if (isIosLivePreview.value || isSelectedStreamReady.value) {
      iosLivePreviewLightDumpCount = 0
      liveHierarchyStatus.value = 'syncing'
      fetchLiveHierarchy({ forceHierarchy: true })
    } else {
      liveNodes.value = []
    }
  } else {
    fetchDump()
  }
  syncLivePreviewPolling({ immediate: Boolean(liveMode.value && selectedSerial.value) })
}

// 切换到投屏模式时自动获取设备列表和层级
watch(liveMode, async (val) => {
  clearQuickImagePrompt()
  cancelQuickImageCapture()
  if (val) {
    liveHierarchyStatus.value = 'idle'
    liveHierarchyError.value = ''
    liveHierarchyLastUpdatedAt.value = 0
    await fetchDevices()
    startLiveStreamPolling()
    if (selectedSerial.value && (isIosLivePreview.value || isSelectedStreamReady.value)) {
      iosLivePreviewLightDumpCount = 0
      liveHierarchyStatus.value = 'syncing'
      bumpLivePreviewPollingBoost()
      fetchLiveHierarchy({ forceHierarchy: true })
    } else {
      liveNodes.value = []
    }
  } else {
    stopLiveStreamPolling()
    stopLivePreviewPolling()
    livePreviewBoostUntil = 0
    if (skipNextStaticDump.value) {
      skipNextStaticDump.value = false
    } else if (selectedSerial.value) {
      fetchDump()
    }
    liveHierarchyStatus.value = 'idle'
    liveHierarchyError.value = ''
    liveHierarchyLastUpdatedAt.value = 0
  }
  syncLivePreviewPolling({ immediate: Boolean(val && selectedSerial.value) })
})

watch(isSelectedStreamReady, (ready) => {
  if (!liveMode.value || isIosLivePreview.value) return
  if (ready && selectedSerial.value) {
    liveHierarchyStatus.value = 'syncing'
    bumpLivePreviewPollingBoost()
    fetchLiveHierarchy({ forceHierarchy: true })
  } else if (!ready) {
    liveHierarchyStatus.value = 'stale'
    liveHierarchyError.value = selectedStreamError.value || '投屏通道未就绪'
  }
  syncLivePreviewPolling({ immediate: ready })
})

watch(
  [liveMode, selectedSerial, selectedRecordingPlatform],
  ([isLive, serial, platform]) => {
    if (isLive && serial && platform === 'ios') {
      iosLivePreviewLightDumpCount = 0
      liveHierarchyStatus.value = 'syncing'
      fetchLiveHierarchy({ forceHierarchy: true })
    }
    syncLivePreviewPolling({ immediate: Boolean(isLive && serial) })
  }
)

// 获取选中设备信息（屏幕尺寸）
const selectedDevice = computed(() => {
  return connectedDevices.value.find(d => d.serial === selectedSerial.value)
})

const parseResolution = (resolution) => {
  const match = String(resolution || '').match(/(\d+)\s*x\s*(\d+)/i)
  if (!match) return { width: 0, height: 0 }
  return {
    width: Number(match[1]) || 0,
    height: Number(match[2]) || 0
  }
}

const selectedDeviceScreenSize = computed(() => parseResolution(selectedDevice.value?.resolution))

// 投屏模式下获取 UI 层级（用于元素高亮）
const fetchLiveHierarchy = async ({ showLoading = false, forceHierarchy = false } = {}) => {
  if (!selectedSerial.value) {
    liveHierarchyStatus.value = 'idle'
    liveHierarchyError.value = ''
    return
  }
  if (liveHierarchyLoading.value) return
  const dumpOptions = getLiveDumpOptions({ forceHierarchy })
  const hierarchyRequested = Boolean(dumpOptions.includeHierarchy)
  if (hierarchyRequested) {
    liveHierarchyStatus.value = 'syncing'
  }
  liveHierarchyLoading.value = true
  if (showLoading) {
    loading.value = true
  }
  try {
    const res = await api.getDeviceDump(selectedSerial.value, dumpOptions)
    updateStateFromDump(res.data)
    if (hierarchyRequested) {
      if (res?.data?.hierarchy_xml) {
        markLiveHierarchyFresh()
      } else {
        markLiveHierarchyStale('层级结果为空')
      }
    }
  } catch (err) {
    console.warn('获取投屏层级失败:', err.response?.data?.detail || err.message)
    markLiveHierarchyStale(err.response?.data?.detail || err.message)
  } finally {
    liveHierarchyLoading.value = false
    if (showLoading) {
      loading.value = false
    }
  }
}

// ScrcpyPlayer 触控事件处理（实时投屏模式下的点击录制）
const onScrcpyTouch = async ({ x, y, relX, relY }) => {
  if (ocrCropMode.value || imageCropMode.value) return  // 框选模式下不触发录制
  if (!ensureInteractionReady(syncMode.value ? '录制' : '探索')) return
  if (loading.value) return
  clearQuickImagePrompt()

  if (!syncMode.value) {
    try {
      await api.sendTouch(selectedSerial.value, 0, x, y)
      liveHierarchyStatus.value = 'syncing'
      liveHierarchyError.value = ''
      bumpLivePreviewPollingBoost()
      ElMessage.success('操作成功')
    } catch (err) {
      console.error('实时投屏交互失败:', err)
      ElMessage.error(getErrorDetail(err, '交互失败'))
    }
    return
  }

  const recordableNode = findBestRecordableNode(liveNodes.value, x, y)
  const fallbackNode = getFallbackNodeAt(liveNodes.value, x, y)
  if (!recordableNode && !fallbackNode) {
    return
  }
  if (!recordableNode && fallbackNode) {
    showLiveQuickImagePrompt({
      node: fallbackNode,
      x,
      y,
      relX,
      relY,
      executeTap: Boolean(syncMode.value)
    })
    return
  }
  loading.value = true

  try {
    const res = await api.interactDevice(x, y, 'click', hierarchyXml.value, null, selectedSerial.value)
    if (res.data.step) {
      caseStore.addStep(res.data.step)
      ElMessage.success('操作成功并添加步骤')
    }
    updateStateFromDump(res.data.dump)
    markLiveHierarchyFresh()
    bumpLivePreviewPollingBoost()
  } catch (err) {
    if (fallbackNode && isQuickImageSuggestedError(err)) {
      showLiveQuickImagePrompt({
        node: fallbackNode,
        x,
        y,
        relX,
        relY,
        executeTap: Boolean(syncMode.value)
      })
    } else {
      console.error('实时投屏交互失败:', err)
      ElMessage.error(getErrorDetail(err, '交互失败'))
    }
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await fetchDevices()
  if (selectedSerial.value) {
    fetchDump()
  }
})

onUnmounted(() => {
  stopLiveStreamPolling()
  stopLivePreviewPolling()
})

const updateStateFromDump = (dump) => {
  if (!dump) return
  if (dump.screenshot) {
    screenshot.value = `data:image/png;base64,${dump.screenshot}`
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

/** 状态标签类型映射 */
const statusTagType = (status) => {
  const map = { IDLE: 'success', BUSY: 'danger', OFFLINE: 'info', WDA_DOWN: 'warning' }
  return map[status] || 'info'
}

/** 状态中文映射 */
const statusLabel = (status) => {
  const map = { IDLE: '🟢 空闲', BUSY: '🔴 执行中', OFFLINE: '⚫ 离线', WDA_DOWN: '🟠 WDA异常' }
  return map[status] || status
}

defineExpose({
  updateStateFromDump,
  refreshDevices: fetchDevices,
  selectedSerial,
  connectedDevices,
  startOcrCrop,
  startImageCrop,
  ocrCropMode,  // 暴露 OCR 框选模式状态
  imageCropMode,  // 暴露图像截取框选模式状态
  activeImageCropStepUuid
})
</script>

<template>
  <div class="device-stage">
    <!-- Toolbar -->
    <div class="stage-toolbar">
      <div class="toolbar-left">
        <slot name="left"></slot>
      </div>
      <div class="toolbar-center">
        <el-select
          v-model="selectedSerial"
          placeholder="当前调试设备"
          class="device-select"
          @change="onDeviceChange"
        >
          <el-option
            v-for="d in recordingDevices"
            :key="d.serial"
            :label="d.custom_name || d.market_name || d.model || d.serial"
            :value="d.serial"
            :disabled="!canObserveDeviceInCurrentMode(d)"
          >
            <div class="device-option-row">
              <span class="device-option-name">{{ d.custom_name || d.market_name || d.model || d.serial }}</span>
              <div class="device-option-status">
                <el-tag :type="statusTagType(d.status)" size="small">{{ statusLabel(d.status) }}</el-tag>
              </div>
            </div>
          </el-option>
        </el-select>
      </div>
      <div class="toolbar-right">
        <slot name="before-refresh"></slot>
        <el-button v-if="liveMode" :icon="Refresh" @click="fetchLiveHierarchy({ showLoading: true, forceHierarchy: true })" :loading="loading" :disabled="!selectedSerial || isSelectedDeviceBusy">刷新层级</el-button>
        <el-button v-if="!liveMode" :icon="Refresh" @click="fetchDump" :loading="loading" :disabled="!selectedSerial || isSelectedDeviceBusy">刷新</el-button>
      </div>
    </div>

    <div class="mode-bar">
      <div class="mode-group">
        <span class="mode-group-label">预览方式</span>
        <el-radio-group v-model="previewMode" size="small" class="mode-segmented">
          <el-radio-button label="static">静态截图</el-radio-button>
          <el-radio-button label="live">实时投屏</el-radio-button>
        </el-radio-group>
      </div>
      <div class="mode-group">
        <span class="mode-group-label">交互方式</span>
        <el-radio-group v-model="interactionMode" size="small" class="mode-segmented" :disabled="isSelectedDeviceBusy">
          <el-radio-button label="explore">探索模式</el-radio-button>
          <el-radio-button label="record">录制模式</el-radio-button>
        </el-radio-group>
      </div>
    </div>

    <!-- 实时投屏模式 -->
    <div
      v-if="liveMode && !isIosLivePreview && selectedSerial && isSelectedStreamReady"
      ref="livePromptHostRef"
      class="live-player-shell"
    >
      <ScrcpyPlayer
        :serial="selectedSerial"
        :record-mode="true"
        :ocr-crop-mode="ocrCropMode"
        :device-width="selectedDeviceScreenSize.width"
        :device-height="selectedDeviceScreenSize.height"
        :nodes="liveNodes"
        @touch="onScrcpyTouch"
      />
      <div
        v-if="quickImagePrompt?.host === 'live' && !isAnyCropMode"
        class="quick-image-prompt"
        :style="{ left: `${quickImagePrompt.left}px`, top: `${quickImagePrompt.top}px` }"
        @mousedown.stop
        @mouseup.stop
        @click.stop
      >
        <div class="quick-image-title">当前区域无 Desc/Text</div>
        <div class="quick-image-desc">可直接转为图像点击录制</div>
        <div class="quick-image-actions">
          <el-button size="small" type="primary" @click.stop="beginQuickImageCapture()">
            改为图像点击
          </el-button>
          <el-button size="small" @click.stop="clearQuickImagePrompt">
            取消
          </el-button>
        </div>
      </div>
    </div>

    <div v-else-if="liveMode && !isIosLivePreview" class="live-placeholder">
      <el-empty
        v-if="!selectedSerial"
        description="请选择调试设备"
        :image-size="80"
      />
      <div v-else class="live-pending">
        <p>{{ selectedStreamError || '投屏通道初始化中，请稍候...' }}</p>
        <el-button size="small" @click="fetchDevices">刷新设备状态</el-button>
      </div>
    </div>

    <!-- 静态截图模式 -->
    <div 
      v-else
      ref="canvasRef"
      class="canvas-container"
      @mousemove="onCanvasMouseMove"
      @mouseleave="onCanvasMouseLeave"
      @mousedown="onCanvasMouseDown"
      @mouseup="onCanvasMouseUp"
      @click="onCanvasClick"
      v-loading="loading"
      element-loading-text="正在执行操作..."
    >
      <img 
        v-if="screenshot" 
        ref="imgRef"
        :src="screenshot" 
        class="device-screenshot"
        draggable="false"
      />
      <div v-else class="no-device">
        <el-empty description="点击刷新获取设备状态" :image-size="80" />
      </div>
      
      <!-- Hover Overlay -->
      <div class="hover-overlay" :style="getOverlayStyle()"></div>
      <div class="crop-overlay" :style="getCropOverlayStyle()"></div>

      <div
        v-if="quickImagePrompt?.host === 'static' && !isAnyCropMode"
        class="quick-image-prompt"
        :style="{ left: `${quickImagePrompt.left}px`, top: `${quickImagePrompt.top}px` }"
        @mousedown.stop
        @mouseup.stop
        @click.stop
      >
        <div class="quick-image-title">当前区域无 Desc/Text</div>
        <div class="quick-image-desc">可直接转为图像点击录制</div>
        <div class="quick-image-actions">
          <el-button size="small" type="primary" @click.stop="beginQuickImageCapture()">
            改为图像点击
          </el-button>
          <el-button size="small" @click.stop="clearQuickImagePrompt">
            取消
          </el-button>
        </div>
      </div>

      <!-- Element Tooltip -->
      <div v-if="hoveredNode && !isAnyCropMode" class="element-tooltip">
        <div v-if="hoveredNode.text" class="tip-row"><b>Text:</b> {{ hoveredNode.text }}</div>
        <div v-if="hoveredNode.resourceId" class="tip-row"><b>ID:</b> {{ hoveredNode.resourceId }}</div>
        <div v-if="hoveredNode.contentDesc" class="tip-row"><b>Desc:</b> {{ hoveredNode.contentDesc }}</div>
        <div class="tip-row"><b>Class:</b> {{ hoveredNode.className }}</div>
      </div>
      <div v-if="isAnyCropMode" class="crop-tip">
        {{
          imageCropMode
            ? (quickImageCapture ? '图像点击快捷模式：可直接确认，或拖拽调整截取区域' : '图像截取模式：按住鼠标左键拖拽选择目标元素区域')
            : 'OCR 框选模式：按住鼠标左键拖拽选择区域'
        }}
      </div>
      <div
        v-if="imageCropMode && quickImageCapture"
        class="quick-image-toolbar"
        @mousedown.stop
        @mouseup.stop
        @click.stop
      >
        <span>将当前操作录成图像点击</span>
        <div class="quick-image-toolbar-actions">
          <el-button size="small" type="primary" @click.stop="confirmQuickImageCapture">
            确认图像点击
          </el-button>
          <el-button size="small" @click.stop="cancelQuickImageCapture">
            取消
          </el-button>
        </div>
      </div>
    </div>


  </div>
</template>

<style scoped>
.device-stage {
  --stage-row-height: 44px;
  --stage-row-padding-y: 4px;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #f5f7fa;
}

.stage-toolbar {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: center;
  column-gap: 12px;
  row-gap: 8px;
  padding: var(--stage-row-padding-y) 20px;
  background: #fff;
  border-bottom: 1px solid #f0f2f5;
  min-height: var(--stage-row-height);
  flex-shrink: 0;
}

.toolbar-left, .toolbar-right {
  display: flex;
  gap: 8px;
  align-items: center;
  min-width: 0;
}

.toolbar-left {
  justify-self: start;
}

.toolbar-center {
  display: flex;
  align-items: center;
  justify-content: center;
  min-width: 0;
  justify-self: center;
}

.toolbar-right {
  min-width: 0;
  flex-wrap: wrap;
  justify-content: flex-end;
  justify-self: end;
}

.mode-bar {
  display: flex;
  justify-content: center;
  align-items: center;
  align-content: center;
  gap: 8px 18px;
  padding: var(--stage-row-padding-y) 20px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  flex-wrap: wrap;
  min-height: var(--stage-row-height);
}

.mode-group {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  min-width: 0;
  min-height: 30px;
}

.mode-group-label {
  font-size: 12px;
  font-weight: 500;
  color: #909399;
  white-space: nowrap;
  line-height: 30px;
}

.mode-segmented {
  --segment-border: #dcdfe6;
  --segment-bg: #ffffff;
  --segment-text: #606266;
  --segment-active-bg: #ecf5ff;
  --segment-active-text: #409eff;
}

.mode-segmented :deep(.el-radio-button__inner) {
  min-width: 74px;
  height: 30px;
  padding: 0 10px;
  line-height: 28px;
  font-size: 12px;
  border: 1px solid var(--segment-border);
  background: var(--segment-bg);
  color: var(--segment-text);
  box-shadow: none;
  transition: all 0.18s ease;
  border-radius: 4px;
}

.mode-segmented :deep(.el-radio-button:first-child .el-radio-button__inner),
.mode-segmented :deep(.el-radio-button:last-child .el-radio-button__inner) {
  border-radius: 4px;
}

.mode-segmented :deep(.el-radio-button__original-radio:checked + .el-radio-button__inner) {
  background: var(--segment-active-bg);
  border-color: var(--segment-active-text);
  color: var(--segment-active-text);
  box-shadow: none;
}

.mode-segmented :deep(.el-radio-button__inner:hover) {
  color: #409eff;
  border-color: #a0cfff;
}

.device-select {
  width: min(220px, 36vw);
}

.device-option-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  gap: 8px;
}

.device-option-name {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.device-option-status {
  margin-left: auto;
  flex-shrink: 0;
}

.device-name {
  color: #606266;
  font-size: 13px;
}

.toolbar-left :deep(.header-left) {
  min-width: 0;
}

@media (max-width: 1280px) {
  .stage-toolbar,
  .mode-bar {
    padding-left: 16px;
    padding-right: 16px;
  }

  .mode-group {
    width: 100%;
    justify-content: flex-start;
  }
}

@media (max-width: 960px) {
  .stage-toolbar {
    grid-template-columns: 1fr;
  }

  .toolbar-left,
  .toolbar-center,
  .toolbar-right {
    width: 100%;
    justify-self: stretch;
  }

  .toolbar-center,
  .mode-bar {
    justify-content: flex-start;
  }

  .device-select {
    width: min(220px, 100%);
  }

  .mode-segmented :deep(.el-radio-button__inner) {
    min-width: 70px;
    height: 30px;
    padding: 0 8px;
    line-height: 28px;
  }
}

.canvas-container {
  flex: 1;
  display: flex;
  justify-content: center;
  align-items: center;
  overflow: hidden;
  position: relative;
  padding: 20px;
}

.live-placeholder {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.live-player-shell {
  position: relative;
  flex: 1;
  min-height: 0;
}

.live-player-shell :deep(.scrcpy-player) {
  height: 100%;
}

.live-pending {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  color: #606266;
}

.live-pending p {
  margin: 0;
}

.device-screenshot {
  max-height: 100%;
  max-width: 100%;
  object-fit: contain;
  border-radius: 8px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.15);
  cursor: crosshair;
}

.no-device {
  color: #909399;
}

.hover-overlay {
  transition: all 0.1s ease;
}

.quick-image-prompt {
  position: absolute;
  width: 248px;
  background: rgba(255, 255, 255, 0.98);
  border: 1px solid rgba(64, 158, 255, 0.28);
  border-radius: 12px;
  box-shadow: 0 14px 32px rgba(15, 23, 42, 0.18);
  padding: 12px;
  z-index: 25;
}

.quick-image-title {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}

.quick-image-desc {
  margin-top: 4px;
  font-size: 12px;
  color: #606266;
}

.quick-image-actions {
  margin-top: 10px;
  display: flex;
  gap: 8px;
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
}

.tip-row {
  margin-bottom: 4px;
  word-break: break-all;
}

.tip-row:last-child {
  margin-bottom: 0;
}

.crop-tip {
  position: absolute;
  top: 20px;
  left: 20px;
  background: rgba(103, 194, 58, 0.92);
  color: #fff;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 12px;
}

.quick-image-toolbar {
  position: absolute;
  top: 20px;
  right: 20px;
  display: flex;
  align-items: center;
  gap: 12px;
  background: rgba(255, 255, 255, 0.96);
  border: 1px solid rgba(64, 158, 255, 0.28);
  border-radius: 10px;
  padding: 10px 12px;
  box-shadow: 0 12px 28px rgba(15, 23, 42, 0.16);
  z-index: 26;
}

.quick-image-toolbar span {
  font-size: 12px;
  color: #303133;
}

.quick-image-toolbar-actions {
  display: flex;
  gap: 8px;
}


</style>
