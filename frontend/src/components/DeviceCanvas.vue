<script setup>
import { ref, onMounted, computed, nextTick } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import api from '@/api'
import { ElMessage } from 'element-plus'

const screenshot = ref('')
const loading = ref(false)
const deviceInfo = ref({})
const imgRef = ref(null)
const hierarchyXml = ref('')
const nodes = ref([])
const hoverRect = ref(null)

const emit = defineEmits(['inspect', 'step-added'])

// Parse bounds string [x1,y1][x2,y2]
const parseBounds = (boundsStr) => {
  const match = boundsStr.match(/\[(\d+),(\d+)\]\[(\d+),(\d+)\]/)
  if (match) {
    return {
      x1: parseInt(match[1]),
      y1: parseInt(match[2]),
      x2: parseInt(match[3]),
      y2: parseInt(match[4])
    }
  }
  return null
}

// Parse XML to flat list of nodes with bounds
const parseHierarchy = (xml) => {
  const parser = new DOMParser()
  const doc = parser.parseFromString(xml, 'text/xml')
  const nodeList = []
  
  const traverse = (node) => {
    if (node.nodeType === 1) { // Element
      const boundsStr = node.getAttribute('bounds')
      if (boundsStr) {
        const bounds = parseBounds(boundsStr)
        if (bounds) {
          nodeList.push({
            bounds,
            class: node.getAttribute('class'),
            resourceId: node.getAttribute('resource-id'),
            text: node.getAttribute('text'),
            contentDesc: node.getAttribute('content-desc'),
             // Calculate area for sorting
            area: (bounds.x2 - bounds.x1) * (bounds.y2 - bounds.y1)
          })
        }
      }
      for (let i = 0; i < node.childNodes.length; i++) {
        traverse(node.childNodes[i])
      }
    }
  }
  
  traverse(doc.documentElement)
  return nodeList
}

const refreshDevice = async () => {
  loading.value = true
  hoverRect.value = null
  try {
    const res = await api.getDeviceDump()
    screenshot.value = `data:image/png;base64,${res.data.screenshot}`
    deviceInfo.value = res.data.device_info
    hierarchyXml.value = res.data.hierarchy_xml
    
    // Parse hierarchy off-thread if huge? Usually fine for < 5MB XML
    nodes.value = parseHierarchy(hierarchyXml.value)
  } catch (err) {
    console.error(err)
    ElMessage.error('获取设备信息失败: ' + err.message)
  } finally {
    loading.value = false
  }
}

// Coordinate mapping helper
const getMappedCoordinates = (clientX, clientY) => {
  if (!imgRef.value) return null
  
  const rect = imgRef.value.getBoundingClientRect()
  const x = clientX - rect.left
  const y = clientY - rect.top
  
  // Bounds check
  if (x < 0 || x > rect.width || y < 0 || y > rect.height) return null

  const scaleX = imgRef.value.naturalWidth / rect.width
  const scaleY = imgRef.value.naturalHeight / rect.height
  
  const realX = Math.round(x * scaleX)
  const realY = Math.round(y * scaleY)
  
  return { realX, realY, scaleX, scaleY, rect }
}

const onMouseMove = (event) => {
  if (!nodes.value.length || !imgRef.value) return
  
  const coords = getMappedCoordinates(event.clientX, event.clientY)
  if (!coords) {
    hoverRect.value = null
    return
  }
  
  const { realX, realY, scaleX, scaleY } = coords
  
  // Find best node locally for hover effect
  // Logic mirrors backend: smallest leaf roughly
  // We can filter by containing point, then sort by area
  const matches = nodes.value.filter(n => 
    realX >= n.bounds.x1 && realX <= n.bounds.x2 &&
    realY >= n.bounds.y1 && realY <= n.bounds.y2
  )
  
  if (matches.length > 0) {
    // Sort by area ascending
    matches.sort((a, b) => a.area - b.area)
    
    // Pick the smallest one
    const best = matches[0]
    
    // Convert back to CSS pixels for overlay
    hoverRect.value = {
      left: best.bounds.x1 / scaleX,
      top: best.bounds.y1 / scaleY,
      width: (best.bounds.x2 - best.bounds.x1) / scaleX,
      height: (best.bounds.y2 - best.bounds.y1) / scaleY
    }
  } else {
    hoverRect.value = null
  }
}

const syncMode = ref(false)
// emit is already defined at top of script

// ... (other refs)

const onCanvasClick = async (event) => {
  if (loading.value) return // Prevent double click
  
  const coords = getMappedCoordinates(event.clientX, event.clientY)
  if (!coords) return
  
  if (!syncMode.value) {
    // Normal Inspect Mode
    emit('inspect', { x: coords.realX, y: coords.realY })
  } else {
    // Sync Mode: Interact and Record
    loading.value = true
    try {
      const res = await api.interactDevice(coords.realX, coords.realY, 'click', hierarchyXml.value)
      
      // Update Device State
      const dump = res.data.dump
      screenshot.value = `data:image/png;base64,${dump.screenshot}`
      deviceInfo.value = dump.device_info
      hierarchyXml.value = dump.hierarchy_xml
      nodes.value = parseHierarchy(hierarchyXml.value)
      
      // Emit Step Added event
      if (res.data.step) {
        emit('step-added', res.data.step)
        ElMessage.success('操作成功并添加步骤')
      }
    } catch (err) {
      console.error(err)
      ElMessage.error('交互失败: ' + err.message)
    } finally {
      loading.value = false
    }
  }
}

onMounted(() => {
  refreshDevice()
})
</script>

<template>
  <el-card class="device-canvas" v-loading="loading">
    <template #header>
      <div class="card-header">
        <span>设备预览</span>
        <div class="header-actions">
           <el-switch
            v-model="syncMode"
            active-text="同步模式"
            inline-prompt
            style="margin-right: 10px"
          />
          <el-button :icon="Refresh" circle size="small" @click="refreshDevice" />
        </div>
      </div>
    </template>
    
    <div 
      class="canvas-content" 
      @mousemove="onMouseMove" 
      @mouseleave="hoverRect = null"
      v-loading="loading"
      element-loading-text="正在执行操作..."
    >
      <div class="image-wrapper">
        <img 
          v-if="screenshot" 
          ref="imgRef"
          :src="screenshot" 
          class="screenshot" 
          alt="Device Screenshot" 
          @click="onCanvasClick"
        />
        <!-- Hover Overlay -->
        <div 
          v-if="hoverRect" 
          class="hover-rect"
          :style="{
            left: hoverRect.left + 'px',
            top: hoverRect.top + 'px',
            width: hoverRect.width + 'px',
            height: hoverRect.height + 'px'
          }"
        ></div>
      </div>

      <div v-if="!screenshot" class="empty-state">
        <el-empty description="未连接设备或未获取截图" />
      </div>
    </div>
    
    <div class="device-info" v-if="deviceInfo.productName">
      <small>{{ deviceInfo.productName }} (SDK: {{ deviceInfo.sdkInt }})</small>
    </div>
  </el-card>
</template>

<style scoped>
.device-canvas {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.canvas-content {
  flex: 1;
  display: flex;
  justify-content: center;
  align-items: center;
  overflow: hidden;
  background-color: #f0f2f5;
  min-height: 400px;
  position: relative;
}

.image-wrapper {
  position: relative;
  /* Ensure overlay is positioned relative to this */
}

.screenshot {
  max-width: 100%;
  max-height: 600px;
  object-fit: contain;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  display: block; /* Remove bottom space */
  cursor: crosshair; 
}

.hover-rect {
  position: absolute;
  border: 2px solid red;
  pointer-events: none; /* Let clicks pass through to img */
  z-index: 10;
  box-shadow: 0 0 4px rgba(255, 0, 0, 0.5);
}

.empty-state {
  display: flex;
  justify-content: center;
}

.device-info {
  margin-top: 10px;
  text-align: center;
  color: #909399;
}
</style>
