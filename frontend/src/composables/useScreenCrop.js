import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import { useCaseStore } from '@/stores/useCaseStore'
import { getErrorDetail } from '@/utils/errors'
import { anchorFromSelection, pointFromAnchor, makeRecordedImageOptions, bindRecordedImage, parseSearchRegion } from '@/utils/imageOptions'

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

const cropTemplateFromBase64 = async (screenshotBase64, selection) => {
  const res = await api.cropTemplate(
    screenshotBase64,
    selection.x1,
    selection.y1,
    selection.x2,
    selection.y2
  )
  return res.data
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
  selectedPlatform,
  getMappedCoordinates,
  ensureInteractionReady,
  executeImageStepAndRefresh,
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
  const imageTapAnchor = ref([0.5, 0.5])
  const imageSearchRegion = ref('')
  const imageMatchResult = ref(null)
  const imageValidationBusy = ref(false)
  const imageTemplate = ref(null)
  const validatedImageKey = ref('')
  const imageOverlayRevision = ref(0)
  let imageValidationRevision = 0
  const currentImagePlatform = () => selectedPlatform?.value === 'ios' ? 'ios' : 'android'

  const invalidateImageValidation = () => {
    imageValidationRevision += 1
    imageMatchResult.value = null
    validatedImageKey.value = ''
  }

  const setImageAnchor = (index, value) => {
    if (value === null || value === undefined || value === '') return
    const number = Number(value)
    if (!Number.isFinite(number) || number < 0 || number > 1) return
    imageTapAnchor.value = imageTapAnchor.value.map((item, i) => i === index ? number : item)
    invalidateImageValidation()
  }

  watch([screenshot, selectedSerial, () => currentImagePlatform()], () => {
    if (imageCropMode.value) invalidateImageValidation()
  }, { flush: 'sync' })
  watch([imageCropMode, imageMatchResult], async () => {
    await nextTick()
    imageOverlayRevision.value += 1
  })

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
    invalidateImageValidation()
    imageTemplate.value = null
    imageSearchRegion.value = ''
    resetCropSelection()
    if (notify) {
      ElMessage.info('已退出图像截取模式')
    }
  }

  const cancelQuickImageCapture = () => {
    stopImageCrop()
  }

  watch(() => imageCropStep.value?.action, action => {
    if (imageCropMode.value && imageCropStep.value && !['click_image', 'assert_image'].includes(action)) stopImageCrop()
  }, { flush: 'sync' })

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

  const addQuickImageStep = (imagePath, imageOptions) => {
    const step = {
      action: 'click_image',
      selector: imagePath,
      selector_type: 'image',
      value: '',
      description: '',
      error_strategy: 'ABORT',
      options: { image_options: imageOptions },
      args: { image_path: imagePath, image_options: imageOptions },
      platform_overrides: { [currentImagePlatform()]: { selector: imagePath, by: 'image' } }
    }
    caseStore.addStep(step)
    return step
  }

  const imageValidationKey = () => JSON.stringify({
    screenshot: screenshot.value, selection: getCurrentCropSelection(), anchor: imageTapAnchor.value,
    region: imageSearchRegion.value, serial: selectedSerial.value, platform: currentImagePlatform(),
    step: imageCropStep.value ? {
      action: imageCropStep.value.action, selector: imageCropStep.value.selector,
      args: imageCropStep.value.args, options: imageCropStep.value.options,
      platform_overrides: imageCropStep.value.platform_overrides
    } : null
  })
  const imageSelectionReady = computed(() => Boolean(imageCropMode.value && getCurrentCropSelection() && !ocrCropDragging.value))
  const canConfirmImage = computed(() => imageMatchResult.value?.status === 'matched'
    && Array.isArray(imageMatchResult.value.point) && imageMatchResult.value.point.length === 2
    && imageMatchResult.value.point.every(value => Number.isFinite(value))
    && validatedImageKey.value === imageValidationKey() && !imageValidationBusy.value)

  const validateImageSelection = async () => {
    const selection = getCurrentCropSelection()
    if (!selection || imageValidationBusy.value || !imageCropMode.value) return false
    invalidateImageValidation()
    const revision = imageValidationRevision
    const key = imageValidationKey()
    imageValidationBusy.value = true
    loading.value = true
    try {
      await api.ensureImageMatcher()
      if (revision !== imageValidationRevision || !imageCropMode.value) return false
      const region = parseSearchRegion(imageSearchRegion.value)
      const templateKey = JSON.stringify({ screenshot: screenshot.value, selection })
      if (imageTemplate.value?.key !== templateKey) {
        const metadata = await cropTemplateFromBase64(getScreenshotBase64(), selection)
        if (revision !== imageValidationRevision) return false
        imageTemplate.value = { key: templateKey, ...metadata }
      }
      const imageOptions = makeRecordedImageOptions(imageTemplate.value, imageTapAnchor.value)
      if (region) imageOptions.search_region = region
      const response = await api.matchImage({
        screenshot_base64: getScreenshotBase64(),
        image_path: imageTemplate.value.image_path,
        image_options: imageOptions
      })
      if (revision !== imageValidationRevision || key !== imageValidationKey()) return false
      imageMatchResult.value = response.data
      validatedImageKey.value = key
      return response.data.status === 'matched'
    } catch (err) {
      if (revision === imageValidationRevision) {
        imageMatchResult.value = { status: 'unsupported', reason: getErrorDetail(err, '图像验证失败，请更新客户端后重试'), candidates: [] }
      }
      return false
    } finally {
      imageValidationBusy.value = false
      loading.value = false
    }
  }

  const finalizeQuickImageStep = async () => {
    if (!canConfirmImage.value || !ensureInteractionReady('保存图像步骤')) return false
    loading.value = true
    let success = false
    try {
      const imagePath = imageTemplate.value.image_path
      const options = makeRecordedImageOptions(imageTemplate.value, imageTapAnchor.value)
      const region = parseSearchRegion(imageSearchRegion.value)
      if (region) options.search_region = region
      const platform = currentImagePlatform()
      const executeTap = Boolean(quickImageCapture.value?.executeTap)
      let recordedStep
      if (imageCropStep.value) {
        bindRecordedImage(imageCropStep.value, platform, imagePath, options)
        recordedStep = imageCropStep.value
      } else {
        recordedStep = addQuickImageStep(imagePath, { [platform]: options })
      }

      let tapError = ''
      if (executeTap) {
        try {
          // Re-localize the live device through the same guarded runtime used by replay.
          await executeImageStepAndRefresh(recordedStep)
        } catch (err) {
          tapError = getErrorDetail(err, '实际点击失败')
        }
      }

      if (tapError) {
        ElMessage.warning(`图像点击步骤已添加，但实际点击失败：${tapError}`)
      } else {
        ElMessage.success(executeTap ? '已保存图像步骤并完成实际点击' : '图像模板及点击位置已保存')
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
    if (imageCropMode.value) invalidateImageValidation()
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

    if (imageCropMode.value) {
      ocrCropStart.value = start
      ocrCropCurrent.value = end
      imageTapAnchor.value = [0.5, 0.5]
      invalidateImageValidation()
      if (event) {
        event.preventDefault()
        event.stopPropagation()
      }
      return
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
    imageTapAnchor.value = anchorFromSelection(initialSelection, { x: draft.x, y: draft.y })
    invalidateImageValidation()
    ElMessage.info('请确认截取区域与红色点击位置，再验证定位')
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

    // iOS 实时模式：底层静态截图是低频兜底（可能落后几秒），截模板前换成当前画面
    if (isIosLivePreview.value) {
      loading.value = true
      try {
        const res = await api.getDeviceDump(selectedSerial.value, {
          includeScreenshot: true,
          includeHierarchy: true,
          includeDeviceInfo: shouldRequestDeviceInfo()
        })
        updateStateFromDump(res.data)
      } catch (err) {
        ElMessage.error('切换图像点击失败: ' + getErrorDetail(err, '获取设备截图失败'))
        return
      } finally {
        loading.value = false
      }
    }

    activateQuickImageCaptureDraft(draft)
  }

  const confirmQuickImageCapture = async () => {
    if (!canConfirmImage.value) {
      ElMessage.warning('请先验证定位，确认唯一目标与点击位置')
      return
    }
    const success = await finalizeQuickImageStep()
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

  const startImageCrop = async (step) => {
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
    if (isIosLivePreview.value) {
      loading.value = true
      try {
        const res = await api.getDeviceDump(selectedSerial.value, {
          includeScreenshot: true, includeHierarchy: false, includeDeviceInfo: shouldRequestDeviceInfo()
        })
        updateStateFromDump(res.data)
      } catch (err) {
        ElMessage.error('获取截取画面失败：' + getErrorDetail(err, '请重试'))
        return
      } finally { loading.value = false }
    }
    ocrCropMode.value = false
    ocrCropStep.value = null
    imageCropMode.value = true
    imageCropStep.value = step
    imageTapAnchor.value = [0.5, 0.5]
    resetCropSelection()
    ElMessage.info('请在截图上按住鼠标拖拽框选目标元素区域')
  }

  const getCropOverlayStyle = () => {
    void imageOverlayRevision.value
    if (!isAnyCropMode.value || !ocrCropStart.value || !ocrCropCurrent.value || !imgRef.value || !canvasRef.value) {
      return { display: 'none' }
    }

    const imgRect = imgRef.value.getBoundingClientRect()
    const canvasRect = canvasRef.value.getBoundingClientRect()
    const offsetX = imgRect.left - canvasRect.left
    const offsetY = imgRect.top - canvasRect.top
    const scaleX = imgRect.width / imgRef.value.naturalWidth
    const scaleY = imgRect.height / imgRef.value.naturalHeight
    const x1 = Math.min(ocrCropStart.value.realX, ocrCropCurrent.value.realX) * scaleX
    const y1 = Math.min(ocrCropStart.value.realY, ocrCropCurrent.value.realY) * scaleY
    const x2 = Math.max(ocrCropStart.value.realX, ocrCropCurrent.value.realX) * scaleX
    const y2 = Math.max(ocrCropStart.value.realY, ocrCropCurrent.value.realY) * scaleY

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

  const getImagePointStyle = () => {
    void imageOverlayRevision.value
    const selection = getCurrentCropSelection()
    if (!imageCropMode.value || !selection || !imgRef.value || !canvasRef.value) return { display: 'none' }
    const [x, y] = imageMatchResult.value?.status === 'matched' && Array.isArray(imageMatchResult.value.point)
      ? imageMatchResult.value.point : pointFromAnchor(selection, imageTapAnchor.value)
    const img = imgRef.value.getBoundingClientRect()
    const canvas = canvasRef.value.getBoundingClientRect()
    return {
      left: `${img.left - canvas.left + x * img.width / imgRef.value.naturalWidth}px`,
      top: `${img.top - canvas.top + y * img.height / imgRef.value.naturalHeight}px`
    }
  }

  const getImageCandidateStyle = (candidate) => {
    void imageOverlayRevision.value
    if (!imgRef.value || !canvasRef.value || !Array.isArray(candidate.rect)) return { display: 'none' }
    const [x, y, width, height] = candidate.rect
    const img = imgRef.value.getBoundingClientRect()
    const canvas = canvasRef.value.getBoundingClientRect()
    return {
      left: `${img.left - canvas.left + x * img.width / imgRef.value.naturalWidth}px`,
      top: `${img.top - canvas.top + y * img.height / imgRef.value.naturalHeight}px`,
      width: `${width * img.width / imgRef.value.naturalWidth}px`,
      height: `${height * img.height / imgRef.value.naturalHeight}px`
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
    imageTapAnchor,
    imageSearchRegion,
    imageMatchResult,
    imageValidationBusy,
    imageSelectionReady,
    canConfirmImage,
    setImageAnchor,
    invalidateImageValidation,
    validateImageSelection,
    getImagePointStyle,
    getImageCandidateStyle,
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
