<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { VideoPlay, Refresh, Monitor } from '@element-plus/icons-vue'
import { useCaseStore } from '@/stores/useCaseStore'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import api from '@/api'
import ScrcpyPlayer from './ScrcpyPlayer.vue'

const caseStore = useCaseStore()
const { running, currentCase } = storeToRefs(caseStore)

// Device state
const screenshot = ref('')
const hierarchyXml = ref('')
const deviceInfo = ref(null)
const nodes = ref([])
const loading = ref(false)
const syncMode = ref(true)

// 实时投屏模式
const liveMode = ref(false)
const connectedDevices = ref([])
const selectedSerial = ref('')
const liveNodes = ref([])  // 投屏模式下的 UI 层级节点



// Hover state
const hoveredNode = ref(null)
const canvasRef = ref(null)
const imgRef = ref(null)
const ocrCropMode = ref(false)
const ocrCropStep = ref(null)
const ocrCropDragging = ref(false)
const ocrCropStart = ref(null)
const ocrCropCurrent = ref(null)
const ocrCropJustCompleted = ref(false)  // 标记框选刚完成
const ocrCropCompletionTimestamp = ref(0)  // 框选完成的时间戳

// Parse XML hierarchy into node list with bounds
const parseHierarchy = (xml) => {
  if (!xml) return []
  const parser = new DOMParser()
  const doc = parser.parseFromString(xml, 'text/xml')
  const result = []
  
  const traverse = (node) => {
    if (node.nodeType === 1 && node.hasAttribute('bounds')) {
      const bounds = node.getAttribute('bounds')
      const match = bounds.match(/\[(\d+),(\d+)\]\[(\d+),(\d+)\]/)
      if (match) {
        result.push({
          x1: parseInt(match[1]),
          y1: parseInt(match[2]),
          x2: parseInt(match[3]),
          y2: parseInt(match[4]),
          text: node.getAttribute('text') || '',
          resourceId: node.getAttribute('resource-id') || '',
          className: node.getAttribute('class') || '',
          contentDesc: node.getAttribute('content-desc') || ''
        })
      }
    }
    for (const child of node.childNodes) {
      traverse(child)
    }
  }
  traverse(doc.documentElement)
  return result
}

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
  let best = null
  let bestArea = Infinity
  
  for (const node of nodes.value) {
    if (realX >= node.x1 && realX <= node.x2 && realY >= node.y1 && realY <= node.y2) {
      const area = (node.x2 - node.x1) * (node.y2 - node.y1)
      if (area < bestArea) {
        best = node
        bestArea = area
      }
    }
  }
  return best
}

// Mouse move handler
const onCanvasMouseMove = (event) => {
  // OCR框选模式下优先处理框选逻辑，不进行元素高亮
  if (ocrCropMode.value) {
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
  if (!ocrCropMode.value) return
  
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

const onCanvasMouseUp = (event) => {
  if (!ocrCropMode.value || !ocrCropDragging.value || !imgRef.value) return

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
    ocrCropStart.value = null
    ocrCropCurrent.value = null
    return
  }

  const imgW = imgRef.value.naturalWidth || 1
  const imgH = imgRef.value.naturalHeight || 1
  const region = [
    Number((x1 / imgW).toFixed(4)),
    Number((y1 / imgH).toFixed(4)),
    Number((x2 / imgW).toFixed(4)),
    Number((y2 / imgH).toFixed(4))
  ]

  if (ocrCropStep.value) {
    ocrCropStep.value.selector = `[${region.join(', ')}]`
    ElMessage.success('OCR 截取区域已回填')
  }

  // 标记框选刚完成，防止触发点击录制
  ocrCropJustCompleted.value = true
  ocrCropCompletionTimestamp.value = Date.now()

  ocrCropMode.value = false
  ocrCropStep.value = null
  ocrCropStart.value = null
  ocrCropCurrent.value = null

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

// Click handler
const onCanvasClick = async (event) => {
  // 检查 OCR 框选模式或刚完成框选
  if (ocrCropMode.value || ocrCropJustCompleted.value) {
    return
  }
  if (loading.value) return

  const coords = getMappedCoordinates(event.clientX, event.clientY)
  if (!coords) return

  if (syncMode.value) {
    // Sync Mode: Interact and Record
    loading.value = true
    try {
      const res = await api.interactDevice(coords.realX, coords.realY, 'click', hierarchyXml.value, null, selectedSerial.value)

      // Update Device State
      const dump = res.data.dump
      screenshot.value = `data:image/png;base64,${dump.screenshot}`
      deviceInfo.value = dump.device_info
      hierarchyXml.value = dump.hierarchy_xml
      nodes.value = parseHierarchy(hierarchyXml.value)

      // Add Step
      if (res.data.step) {
        caseStore.addStep(res.data.step)
        ElMessage.success('操作成功并添加步骤')
      }
    } catch (err) {
      console.error(err)
      ElMessage.error('交互失败: ' + err.message)
    } finally {
      loading.value = false
    }
  } else {
    // Inspect Mode: 审查模式 - 不执行操作，只添加步骤
    loading.value = true
    try {
      const res = await api.inspectDevice(coords.realX, coords.realY, selectedSerial.value)

      // 使用后端返回的 step（与同步模式格式一致）
      if (res.data.step) {
        caseStore.addStep(res.data.step)
        ElMessage.success('已添加步骤')
      } else {
        ElMessage.warning('未能识别元素')
      }
    } catch (err) {
      ElMessage.error('识别元素失败: ' + (err.response?.data?.detail || err.message))
    } finally {
      loading.value = false
    }
  }
}

const startOcrCrop = (step) => {
  if (liveMode.value) {
    ElMessage.warning('请切换到静态截图模式后再进行框选')
    return
  }
  if (!screenshot.value) {
    ElMessage.warning('当前无设备截图，请先刷新')
    return
  }
  ocrCropMode.value = true
  ocrCropStep.value = step
  ocrCropDragging.value = false
  ocrCropStart.value = null
  ocrCropCurrent.value = null
  ElMessage.info('请在截图上按住鼠标拖拽框选 OCR 区域')
}

const getCropOverlayStyle = () => {
  if (!ocrCropMode.value || !ocrCropStart.value || !ocrCropCurrent.value || !imgRef.value || !canvasRef.value) {
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
    border: '2px dashed #67c23a',
    backgroundColor: 'rgba(103, 194, 58, 0.18)',
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
  loading.value = true
  try {
    const res = await api.getDeviceDump(selectedSerial.value)
    screenshot.value = `data:image/png;base64,${res.data.screenshot}`
    hierarchyXml.value = res.data.hierarchy_xml
    deviceInfo.value = res.data.device_info
    nodes.value = parseHierarchy(hierarchyXml.value)
  } catch (err) {
    ElMessage.error('获取设备状态失败: ' + err.message)
  } finally {
    loading.value = false
  }
}



// 获取设备列表
const fetchDevices = async () => {
  try {
    const res = await api.getDeviceList()
    connectedDevices.value = res.data
    // 自动选择第一台空闲设备
    const idleDevice = connectedDevices.value.find(d => d.status === 'IDLE')
    if (idleDevice && !selectedSerial.value) {
      selectedSerial.value = idleDevice.serial
    }
  } catch (err) {
    console.error('获取设备列表失败:', err)
  }
}

// 当选择设备变化时,根据当前模式刷新
const onDeviceChange = async () => {
  // 先刷新设备列表,获取最新状态
  await fetchDevices()
  
  // 根据模式刷新设备内容
  if (liveMode.value) {
    fetchLiveHierarchy()
  } else {
    fetchDump()
  }
}

// 切换到投屏模式时自动获取设备列表和层级
watch(liveMode, (val) => {
  if (val) {
    if (connectedDevices.value.length === 0) fetchDevices()
    fetchLiveHierarchy()
  } else {
    fetchDump()
  }
})

// 获取选中设备信息（屏幕尺寸）
const selectedDevice = computed(() => {
  return connectedDevices.value.find(d => d.serial === selectedSerial.value)
})

// 投屏模式下获取 UI 层级（用于元素高亮）
const fetchLiveHierarchy = async () => {
  try {
    const res = await api.getDeviceDump(selectedSerial.value)
    const xml = res.data.hierarchy_xml
    liveNodes.value = parseHierarchy(xml)
  } catch (err) {
    console.warn('获取投屏层级失败:', err.message)
    liveNodes.value = []
  }
}

// ScrcpyPlayer 触控事件处理（实时投屏模式下的点击录制）
const onScrcpyTouch = async ({ x, y }) => {
  if (ocrCropMode.value) return  // OCR框选模式下不触发录制
  if (loading.value) return
  loading.value = true

  try {
    if (syncMode.value) {
      const res = await api.interactDevice(x, y, 'click', '', null, selectedSerial.value)
      if (res.data.step) {
        caseStore.addStep(res.data.step)
        ElMessage.success('操作成功并添加步骤')
      }
    } else {
      const res = await api.inspectDevice(x, y, selectedSerial.value)
      if (res.data.step) {
        caseStore.addStep(res.data.step)
        ElMessage.success('已添加步骤')
      } else {
        ElMessage.warning('未能识别元素')
      }
    }
    // 交互后刷新层级（UI 可能已变化）
    fetchLiveHierarchy()
  } catch (err) {
    console.error('实时投屏交互失败:', err)
    ElMessage.error('交互失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await fetchDevices()
  fetchDump()
})

const updateStateFromDump = (dump) => {
  if (!dump) return
  screenshot.value = `data:image/png;base64,${dump.screenshot}`
  hierarchyXml.value = dump.hierarchy_xml
  deviceInfo.value = dump.device_info
  nodes.value = parseHierarchy(hierarchyXml.value)
}

/** 状态标签类型映射 */
const statusTagType = (status) => {
  const map = { IDLE: 'success', BUSY: 'danger', OFFLINE: 'info' }
  return map[status] || 'info'
}

/** 状态中文映射 */
const statusLabel = (status) => {
  const map = { IDLE: '🟢 空闲', BUSY: '🔴 执行中', OFFLINE: '⚫ 离线' }
  return map[status] || status
}

defineExpose({
  updateStateFromDump,
  selectedSerial,
  connectedDevices,
  startOcrCrop,
  ocrCropMode  // 暴露 OCR 框选模式状态
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
        <el-switch
          v-model="liveMode"
          active-text="实时投屏"
          inactive-text="静态截图"
          inline-prompt
          style="--el-switch-on-color: #e74c3c; --el-switch-off-color: #909399;"
        />
        <el-select
          v-model="selectedSerial"
          placeholder="当前调试设备"
          class="device-select"
          @change="onDeviceChange"
        >
          <el-option
            v-for="d in connectedDevices"
            :key="d.serial"
            :label="d.custom_name || d.market_name || d.model || d.serial"
            :value="d.serial"
            :disabled="d.status !== 'IDLE'"
          >
            <span>{{ d.custom_name || d.market_name || d.model || d.serial }}</span>
            <el-tag :type="statusTagType(d.status)" size="small">{{ statusLabel(d.status) }}</el-tag>
          </el-option>
        </el-select>
      </div>
      <div class="toolbar-right">
        <slot name="before-refresh"></slot>
        <el-button v-if="liveMode" :icon="Refresh" @click="fetchLiveHierarchy" :loading="loading">刷新层级</el-button>
        <el-button v-if="!liveMode" :icon="Refresh" @click="fetchDump" :loading="loading">刷新</el-button>
      </div>
    </div>

    <!-- 实时投屏模式 -->
    <ScrcpyPlayer
      v-if="liveMode && selectedSerial"
      :serial="selectedSerial"
      :record-mode="true"
      :ocr-crop-mode="ocrCropMode"
      :device-width="selectedDevice?.screen_width || 0"
      :device-height="selectedDevice?.screen_height || 0"
      :nodes="liveNodes"
      @touch="onScrcpyTouch"
    />

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

      <!-- Element Tooltip -->
      <div v-if="hoveredNode && !ocrCropMode" class="element-tooltip">
        <div v-if="hoveredNode.text" class="tip-row"><b>Text:</b> {{ hoveredNode.text }}</div>
        <div v-if="hoveredNode.resourceId" class="tip-row"><b>ID:</b> {{ hoveredNode.resourceId }}</div>
        <div v-if="hoveredNode.contentDesc" class="tip-row"><b>Desc:</b> {{ hoveredNode.contentDesc }}</div>
        <div class="tip-row"><b>Class:</b> {{ hoveredNode.className }}</div>
      </div>
      <div v-if="ocrCropMode" class="crop-tip">OCR 框选模式：按住鼠标左键拖拽选择区域</div>
    </div>


  </div>
</template>

<style scoped>
.device-stage {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #f5f7fa;
}

.stage-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 20px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  height: 50px;
  flex-shrink: 0;
  position: relative; /* For absolute centering */
}

.toolbar-left, .toolbar-right {
  display: flex;
  gap: 10px;
  align-items: center;
}

.toolbar-center {
  display: flex;
  align-items: center;
  gap: 12px;
}

.device-select {
  width: 110px;
}

.device-name {
  color: #606266;
  font-size: 13px;
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


</style>
