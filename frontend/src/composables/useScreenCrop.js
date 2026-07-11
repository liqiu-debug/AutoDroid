import { computed, nextTick, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import { useCaseStore } from '@/stores/useCaseStore'
import { getErrorDetail } from '@/utils/errors'

const QUICK_IMAGE_PROMPT_WIDTH = 248
const QUICK_IMAGE_HINT_KEYWORDS = ['无 Desc/Text', '图像点击', '未识别到可录制元素']

export const isQuickImageSuggestedError = (err) => {
  const detail = getErrorDetail(err, '')
  return QUICK_IMAGE_HINT_KEYWORDS.some(keyword => detail.includes(keyword))
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

const resolveQuickImagePromptPosition = (hostWidth, hostHeight, baseLeft, baseTop) => {
  const maxLeft = Math.max(16, hostWidth - QUICK_IMAGE_PROMPT_WIDTH - 16)
  const maxTop = Math.max(16, hostHeight - 116)
  return {
    left: Math.min(Math.max(16, baseLeft + 16), maxLeft),
    top: Math.min(Math.max(16, baseTop + 16), maxTop)
  }
}

/**
 * 截图框选与快捷图像点击录制状态机：
 * - OCR 区域框选、图像模板框选两种模式的拖拽选区
 * - "无 Desc/Text" 元素的快捷图像点击提示与转换流程
 * - 选区几何换算与遮罩样式
 */
export function useScreenCrop({
  canvasRef,
  imgRef,
  screenshot,
  loading,
  liveMode,
  isIosLivePreview,
  selectedSerial,
  getMappedCoordinates,
  ensureInteractionReady,
  executeRawTapAndRefresh,
  updateStateFromDump,
  shouldRequestDeviceInfo
}) {
  const caseStore = useCaseStore()

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

  // 判断是否处于任何框选模式
  const isAnyCropMode = computed(() => ocrCropMode.value || imageCropMode.value)
  const activeImageCropStepUuid = computed(() => {
    if (!imageCropMode.value || quickImageCapture.value) return ''
    return String(imageCropStep.value?.uuid || '').trim()
  })

  const resetCropSelection = () => {
    ocrCropDragging.value = false
    ocrCropStart.value = null
    ocrCropCurrent.value = null
  }

  const clearQuickImagePrompt = () => {
    quickImagePrompt.value = null
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

  const addQuickImageStep = (imagePath, node) => {
    caseStore.addStep({
      action: 'click_image',
      selector: imagePath,
      selector_type: 'image',
      value: '',
      description: '',
      error_strategy: 'ABORT'
    })
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

  return {
    livePromptHostRef,
    ocrCropMode,
    ocrCropStep,
    ocrCropDragging,
    ocrCropCurrent,
    ocrCropJustCompleted,
    imageCropMode,
    imageCropStep,
    quickImagePrompt,
    quickImageCapture,
    skipNextStaticDump,
    isAnyCropMode,
    activeImageCropStepUuid,
    resetCropSelection,
    clearQuickImagePrompt,
    stopImageCrop,
    cancelQuickImageCapture,
    onCanvasMouseDown,
    onCanvasMouseUp,
    showQuickImagePrompt,
    showLiveQuickImagePrompt,
    beginQuickImageCapture,
    confirmQuickImageCapture,
    startOcrCrop,
    startImageCrop,
    getCropOverlayStyle
  }
}
