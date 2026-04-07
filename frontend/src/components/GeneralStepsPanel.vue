<script setup>
import { computed, ref } from 'vue'
import { VideoPlay, Close, Back, House, Timer, Top, Bottom, Rank } from '@element-plus/icons-vue'
import { VueDraggable } from 'vue-draggable-plus'
import api from '@/api'
import { useCaseStore } from '@/stores/useCaseStore'
import { ElMessage } from 'element-plus'
import { createUuid } from '@/utils/uuid'

// Props & Store
const props = defineProps({
  loading: Boolean,
  deviceSerial: {
    type: String,
    default: ''
  }
})

const caseStore = useCaseStore()
const packageName = ref('com.ehaier.zgq.shop.mall')
const emit = defineEmits(['action-start', 'action-end', 'refresh-needed'])
const hasSelectedDevice = computed(() => !!props.deviceSerial)

// Pre-defined draggable steps
const draggableSteps = ref([
  { action: 'start_app', selector: 'com.ehaier.zgq.shop.mall', description: '启动应用', icon: VideoPlay },
  { action: 'stop_app', selector: 'com.ehaier.zgq.shop.mall', description: '停止应用', icon: Close },
  { action: 'back', selector: '', description: '返回', icon: Back },
  { action: 'home', selector: '', description: '主页', icon: House },
  { action: 'swipe', selector: 'up', description: '上滑', icon: Top },
  { action: 'swipe', selector: 'down', description: '下滑', icon: Bottom },
  { action: 'sleep', selector: '', value: '5', description: '强制等待 5 秒', icon: Timer },
  { action: 'wait_until_exists', selector: '', description: '等待元素', icon: Timer },
])

const handleClone = (item) => {
  return {
    uuid: createUuid(),
    action: item.action,
    selector: item.selector,
    selector_type: 'text',
    value: item.value || '',
    description: item.description,
    timeout: 10,
    error_strategy: 'ABORT',
    execute_on: ['android', 'ios'],
    platform_overrides: {
      android: null,
      ios: null
    }
  }
}

// Execute Action Immediately
const executeAction = async (action, data = '') => {
  if (props.loading) return
  if (!props.deviceSerial) {
    ElMessage.warning('请先选择一台调试设备')
    return
  }
  
  // Specific checks
  if (action === 'start_app' && !packageName.value) {
    ElMessage.warning('请输入包名')
    return
  }
  if (action === 'stop_app' && !packageName.value) {
    ElMessage.warning('请输入包名')
    return
  }

  const finalData = (action === 'start_app' || action === 'stop_app') ? packageName.value : data
  
  emit('action-start')
  try {
    const res = await api.interactDevice(0, 0, action, null, finalData, props.deviceSerial)
    
    if (res.data.step) {
      caseStore.addStep(res.data.step)
      ElMessage.success(`执行成功: ${action}`)
    }
    emit('refresh-needed', res.data.dump) // Pass back new device state
  } catch (err) {
    console.error(err)
    ElMessage.error('执行失败: ' + err.message)
  } finally {
    emit('action-end')
  }
}

</script>

<template>
  <div class="general-panel">
    <div class="panel-header">通用步骤</div>
    <div v-if="!hasSelectedDevice" class="panel-hint">请先在中间设备区选择调试设备，才可执行录制动作。</div>
    
    <div class="panel-section">
      <div class="section-title">应用管理</div>
      <el-input 
        v-model="packageName" 
        placeholder="输入包名 / Bundle ID" 
        size="small"
        clearable
        class="pkg-input"
      />
      <div class="btn-grid">
        <el-button size="small" :icon="VideoPlay" @click="executeAction('start_app')" :disabled="!hasSelectedDevice">启动</el-button>
        <el-button size="small" :icon="Close" @click="executeAction('stop_app')" :disabled="!hasSelectedDevice">停止</el-button>
      </div>
    </div>

    <div class="panel-section">
      <div class="section-title">导航控制</div>
      <div class="btn-grid">
        <el-button size="small" :icon="Back" @click="executeAction('back')" :disabled="!hasSelectedDevice">返回</el-button>
        <el-button size="small" :icon="House" @click="executeAction('home')" :disabled="!hasSelectedDevice">主页</el-button>
      </div>
    </div>

    <div class="panel-section">
      <div class="section-title">滑动操作</div>
      <div class="btn-grid">
        <el-button size="small" :icon="Top" @click="executeAction('swipe', 'up')" :disabled="!hasSelectedDevice">上滑</el-button>
        <el-button size="small" :icon="Bottom" @click="executeAction('swipe', 'down')" :disabled="!hasSelectedDevice">下滑</el-button>
      </div>
    </div>

    <div class="panel-section">
      <div class="section-title">
        <el-icon><Rank /></el-icon> 拖拽添加 (不执行)
      </div>
      <VueDraggable
        v-model="draggableSteps"
        :group="{ name: 'steps', pull: 'clone', put: false }"
        :clone="handleClone"
        :sort="false"
        class="drag-list"
      >
        <div v-for="item in draggableSteps" :key="item.action + item.selector" class="drag-item">
          <component :is="item.icon" class="item-icon" />
          <span>{{ item.description }}</span>
        </div>
      </VueDraggable>
    </div>
  </div>
</template>

<style scoped>
.general-panel {
  height: 100%;
  background: #fff;
  border-left: 1px solid #e4e7ed;
  display: flex;
  flex-direction: column;
}

.panel-header {
  padding: 12px 20px;
  font-weight: 600;
  font-size: 14px;
  border-bottom: 1px solid #ebeef5;
  background: #fafafa;
  display: flex;
  align-items: center;
  box-sizing: border-box;
  height: 50px; /* Match DeviceStage header height */
  flex-shrink: 0;
}

.panel-hint {
  margin: 8px 12px 0;
  padding: 8px 10px;
  font-size: 12px;
  color: #e6a23c;
  background: #fdf6ec;
  border: 1px solid #f5dab1;
  border-radius: 6px;
}

.panel-section {
  padding: 12px;
  border-bottom: 1px solid #f2f6fc;
}

.section-title {
  font-size: 12px;
  color: #909399;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.pkg-input {
  margin-bottom: 8px;
}

.btn-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}

.btn-grid .el-button {
  margin: 0;
}

.drag-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.drag-item {
  display: flex;
  align-items: center;
  padding: 8px;
  background: #f5f7fa;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  cursor: grab;
  font-size: 12px;
  color: #606266;
  gap: 8px;
}

.drag-item:hover {
  background: #ecf5ff;
  border-color: #c6e2ff;
  color: #409eff;
}

.drag-item:active {
  cursor: grabbing;
}

.item-icon {
  width: 14px;
  height: 14px;
}
</style>
