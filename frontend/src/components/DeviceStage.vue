<script setup>
import { ref, computed, onMounted, onUnmounted, onBeforeUnmount, onDeactivated, onActivated, watch } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { useCaseStore } from '@/stores/useCaseStore'
import { ElMessage } from 'element-plus'
import api from '@/api'
import { findBestRecordableNode } from '@/utils/recordableNode'
import { getErrorDetail } from '@/utils/errors'
import { useStageLifecycle } from '@/composables/useStageLifecycle'
import { useDeviceStreams } from '@/composables/useDeviceStreams'
import { useDeviceDump } from '@/composables/useDeviceDump'
import { useStageCanvas } from '@/composables/useStageCanvas'
import { useScreenCrop, isQuickImageSuggestedError } from '@/composables/useScreenCrop'
import { useLivePreviewPolling } from '@/composables/useLivePreviewPolling'
import ScrcpyPlayer from './ScrcpyPlayer.vue'
import IosMjpegPlayer from './IosMjpegPlayer.vue'

const caseStore = useCaseStore()

const syncMode = ref(false)
const liveMode = ref(false)

// 生命周期守卫 + dump 请求取消
const stage = useStageLifecycle()
const { isStageActive, cancelActiveDumpRequests } = stage

// 设备选择与投屏流通道
const {
  connectedDevices,
  selectedSerial,
  recordingDevices,
  selectedRecordingDevice,
  selectedRecordingPlatform,
  isSelectedDeviceBusy,
  isIosLivePreview,
  isSelectedStreamReady,
  liveStreamReconnectInFlight,
  liveStreamReconnectError,
  selectedStreamError,
  selectedStreamPendingText,
  selectedDeviceScreenSize,
  canObserveDeviceInCurrentMode,
  fetchDevices,
  startLiveStreamPolling,
  stopLiveStreamPolling,
  retryLiveStreamChannel
} = useDeviceStreams({ liveMode, isStageActive })

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

// 截图 / 层级状态与刷新
const dump = useDeviceDump({
  stage,
  selectedSerial,
  isIosLivePreview,
  onBeforeFetchDump: () => clearQuickImagePrompt()
})
const {
  screenshot,
  hierarchyXml,
  hierarchyHash,
  nodes,
  liveNodes,
  loading,
  liveHierarchyLoading,
  liveHierarchyStatus,
  liveHierarchyError,
  resetIosLightDumpCount,
  markLiveHierarchyFresh,
  resetLiveHierarchyMeta,
  updateStateFromDump,
  fetchDump,
  fetchLiveHierarchy
} = dump

// 画布坐标换算与悬停高亮
const {
  canvasRef,
  imgRef,
  hoveredNode,
  getCurrentNodeList,
  getMappedCoordinates,
  findNodeAt,
  getFallbackNodeAt,
  onCanvasMouseLeave,
  getOverlayStyle
} = useStageCanvas({ liveMode, nodes, liveNodes })

const executeRawTapAndRefresh = async (x, y) => {
  const res = await api.interactDevice(x, y, 'click', hierarchyXml.value, null, selectedSerial.value, false)
  if (res?.data?.dump) {
    updateStateFromDump(res.data.dump)
  }
  return res
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

// 框选与快捷图像点击录制状态机
const {
  livePromptHostRef,
  ocrCropMode,
  ocrCropDragging,
  ocrCropCurrent,
  ocrCropJustCompleted,
  imageCropMode,
  quickImagePrompt,
  quickImageCapture,
  skipNextStaticDump,
  isAnyCropMode,
  activeImageCropStepUuid,
  clearQuickImagePrompt,
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
} = useScreenCrop({
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
  updateStateFromDump: dump.updateStateFromDump,
  shouldRequestDeviceInfo: dump.shouldRequestDeviceInfo
})

// 实时预览层级轮询
const {
  startLivePreviewPolling,
  stopLivePreviewPolling,
  syncLivePreviewPolling,
  bumpLivePreviewPollingBoost,
  resetLivePreviewBoost
} = useLivePreviewPolling({
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
})

const teardownStagePolling = () => {
  stopLiveStreamPolling()
  stopLivePreviewPolling()
  cancelActiveDumpRequests()
}

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

// ScrcpyPlayer 触控事件处理（实时投屏模式下的点击录制）
const onScrcpyTouch = async ({ x, y, relX, relY }) => {
  if (ocrCropMode.value || imageCropMode.value) return  // 框选模式下不触发录制
  if (!ensureInteractionReady(syncMode.value ? '录制' : '探索')) return
  if (loading.value) return
  clearQuickImagePrompt()

  if (!syncMode.value) {
    try {
      await api.sendTouch(selectedSerial.value, 0, x, y, { method: 'adb' })
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

// 当选择设备变化时,根据当前模式刷新
const onDeviceChange = async () => {
  // 先刷新设备列表,获取最新状态
  clearQuickImagePrompt()
  cancelQuickImageCapture()
  liveStreamReconnectError.value = ''
  hierarchyXml.value = ''
  hierarchyHash.value = ''
  nodes.value = []
  liveNodes.value = []
  resetLiveHierarchyMeta()
  await fetchDevices()

  if (!selectedSerial.value) {
    resetLivePreviewBoost()
    hierarchyXml.value = ''
    hierarchyHash.value = ''
    liveNodes.value = []
    return
  }

  // 根据模式刷新设备内容
  if (liveMode.value) {
    if (isIosLivePreview.value || isSelectedStreamReady.value) {
      resetIosLightDumpCount()
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
    resetLiveHierarchyMeta()
    await fetchDevices()
    startLiveStreamPolling()
    if (selectedSerial.value && (isIosLivePreview.value || isSelectedStreamReady.value)) {
      resetIosLightDumpCount()
      liveHierarchyStatus.value = 'syncing'
      bumpLivePreviewPollingBoost()
      fetchLiveHierarchy({ forceHierarchy: true })
    } else {
      liveNodes.value = []
    }
  } else {
    stopLiveStreamPolling()
    stopLivePreviewPolling()
    resetLivePreviewBoost()
    liveStreamReconnectError.value = ''
    if (skipNextStaticDump.value) {
      skipNextStaticDump.value = false
    } else if (selectedSerial.value) {
      fetchDump()
    }
    resetLiveHierarchyMeta()
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
      resetIosLightDumpCount()
      liveHierarchyStatus.value = 'syncing'
      fetchLiveHierarchy({ forceHierarchy: true })
    }
    syncLivePreviewPolling({ immediate: Boolean(isLive && serial) })
  }
)

onMounted(async () => {
  isStageActive.value = true
  await fetchDevices()
  if (isStageActive.value && selectedSerial.value) {
    fetchDump()
  }
})

onActivated(() => {
  isStageActive.value = true
  if (liveMode.value) {
    startLiveStreamPolling()
    fetchDevices()
  }
  syncLivePreviewPolling({ immediate: Boolean(liveMode.value && selectedSerial.value) })
})

onBeforeUnmount(() => {
  isStageActive.value = false
  teardownStagePolling()
})

onDeactivated(() => {
  isStageActive.value = false
  teardownStagePolling()
})

onUnmounted(() => {
  teardownStagePolling()
})

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
  ocrCropMode,
  imageCropMode,
  activeImageCropStepUuid,
  syncMode
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
        <el-button v-if="liveMode" :icon="Refresh" @click="fetchLiveHierarchy({ showLoading: true, forceHierarchy: true })" :loading="loading" :disabled="!selectedSerial || isSelectedDeviceBusy">{{ isIosLivePreview ? '刷新截图/层级' : '刷新层级' }}</el-button>
        <el-button v-if="!liveMode" :icon="Refresh" @click="fetchDump" :loading="loading" :disabled="!selectedSerial || isSelectedDeviceBusy">刷新</el-button>
      </div>
    </div>

    <div class="mode-bar">
      <div class="mode-group">
        <span class="mode-group-label">预览方式</span>
        <el-radio-group v-model="previewMode" size="small" class="mode-segmented">
          <el-radio-button value="static">静态截图</el-radio-button>
          <el-radio-button value="live">实时投屏</el-radio-button>
        </el-radio-group>
      </div>
      <div class="mode-group">
        <span class="mode-group-label">交互方式</span>
        <el-radio-group v-model="interactionMode" size="small" class="mode-segmented" :disabled="isSelectedDeviceBusy">
          <el-radio-button value="explore">探索模式</el-radio-button>
          <el-radio-button value="record">录制模式</el-radio-button>
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
        <p>{{ selectedStreamPendingText }}</p>
        <el-button size="small" :loading="liveStreamReconnectInFlight" @click="retryLiveStreamChannel">重试投屏通道</el-button>
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

      <!--
        iOS 实时画面层（WDA MJPEG）：只读预览，鼠标事件穿透到底层静态画布。
        点击 / 悬停 / 框选仍基于"静态截图 + 层级"坐标系，保证录制正确性；
        框选或快捷图像提示期间隐藏实时层，露出真实的截取源画面（静态截图）。
      -->
      <IosMjpegPlayer
        v-if="isIosLivePreview"
        v-show="!isAnyCropMode && !quickImagePrompt"
        class="ios-live-layer"
        :serial="selectedSerial"
        @wda-unavailable="fetchDevices()"
      />

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

/*
 * iOS 实时画面层：与 .canvas-container 保持相同的内边距，
 * 使实时帧的 contain 适配约束盒与底层静态截图完全一致，
 * 悬停高亮 / 框选遮罩（按静态截图坐标计算）在视觉上自然对齐。
 */
.ios-live-layer {
  padding: 20px;
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
