<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { Upload, VideoPlay, Back } from '@element-plus/icons-vue'
import { useRoute, useRouter } from 'vue-router'
import DeviceStage from '@/components/DeviceStage.vue'
import StepBuilder from '@/components/StepBuilder.vue'
import LogConsole from '@/components/LogConsole.vue'
import GeneralStepsPanel from '@/components/GeneralStepsPanel.vue'
import { useCaseStore } from '@/stores/useCaseStore'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'

const route = useRoute()
const router = useRouter()
const caseStore = useCaseStore()
const { currentCase, loading } = storeToRefs(caseStore)

const logConsoleRef = ref(null)
const deviceStageRef = ref(null)
const isRunning = ref(false)
const envId = ref(null)
const environments = ref([])

// 获取 DeviceStage 的 OCR 框选模式状态
const ocrCropMode = computed(() => {
  return deviceStageRef.value?.ocrCropMode?.value || false
})

const initData = async () => {
    const id = route.params.id
    if (id) {
        await caseStore.loadCase(id)
    } else {
        const folderId = route.query.folder_id ? Number(route.query.folder_id) : null
        caseStore.newCase({ folder_id: folderId })
    }
    try {
        const { data } = await api.getEnvironments()
        environments.value = data
        if (data && data.length > 0 && !envId.value) {
            envId.value = data[0].id
        }
    } catch (error) {
        console.error('获取环境列表失败', error)
    }
}

watch(() => route.params.id, () => {
    initData()
})

const goBack = () => {
    router.push('/ui/cases')
}

const handleRun = async () => {
  if (!currentCase.value.id) {
    ElMessage.warning('请先保存用例')
    return
  }
  
  if (currentCase.value.steps.length === 0) {
    ElMessage.warning('用例没有步骤')
    return
  }
  
  const currentDevice = deviceStageRef.value?.selectedSerial
  isRunning.value = true
  logConsoleRef.value?.connect(currentCase.value.id, envId.value, currentDevice)
}

const runDialogVisible = ref(false)
const multiRunForm = ref({
  deviceSerials: []
})
const submitMultiRun = async () => {
  if (multiRunForm.value.deviceSerials.length === 0) {
    ElMessage.warning('请至少选择一台设备')
    return
  }
  try {
    const promises = multiRunForm.value.deviceSerials.map(serial => 
      api.runTestCaseAsync(currentCase.value.id, envId.value, serial)
    )
    await Promise.all(promises)
    ElMessage.success(`后台已开始在 ${promises.length} 台设备上执行用例`)
    runDialogVisible.value = false
  } catch (err) {
    ElMessage.error('启动批量执行失败: ' + err.message)
  }
}

const handleRunCommand = (command) => {
  if (command === 'multi') {
    if (!currentCase.value.id) {
      ElMessage.warning('请先保存用例')
      return
    }
    if (currentCase.value.steps.length === 0) {
      ElMessage.warning('用例没有步骤')
      return
    }
    multiRunForm.value.deviceSerials = deviceStageRef.value?.selectedSerial ? [deviceStageRef.value.selectedSerial] : []
    runDialogVisible.value = true
  }
}

const handleRunComplete = (data) => {
  isRunning.value = false
  if (data.success) {
    ElMessage.success(`执行完成: ${data.passed} 通过`)
  } else {
    ElMessage.error(`执行失败: ${data.failed} 个步骤失败`)
  }
}

const handleRefreshNeeded = (dumpData) => {
  if (deviceStageRef.value) {
    deviceStageRef.value.updateStateFromDump(dumpData)
  }
}

const handleRequestOcrCrop = (step) => {
  if (deviceStageRef.value?.startOcrCrop) {
    deviceStageRef.value.startOcrCrop(step)
  }
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

onMounted(() => {
    initData()
})
</script>

<template>
  <el-container class="main-layout">
    <!-- Global Header is in App.vue -->
    
    <el-container class="content-container">
      <!-- Removed CaseExplorer (Left Pane) -->
      
      <el-main class="center-pane">
        <div class="center-wrapper">
          <DeviceStage 
            ref="deviceStageRef"
            :env-id="envId"
            @update-loading="loading = $event"
          >
            <template #left>
              <div class="header-left">
                  <el-button :icon="Back" link @click="goBack" class="back-btn" />
                  <div class="logo">
                     <el-input 
                       v-model="currentCase.name" 
                       placeholder="请输入用例名称" 
                       class="title-input"
                     />
                  </div>
              </div>
            </template>
            <template #before-refresh>
              <el-select
                v-model="envId"
                placeholder="运行环境"
                style="width: 85px;"
              >
                <el-option
                  v-for="env in environments"
                  :key="env.id"
                  :label="env.name"
                  :value="env.id"
                />
              </el-select>
            </template>
          </DeviceStage>
          <LogConsole 
            ref="logConsoleRef" 
            :case-id="currentCase.id"
            @run-complete="handleRunComplete"
          />
        </div>
      </el-main>
      
      <el-aside width="220px" class="general-pane">
        <GeneralStepsPanel
          :loading="loading"
          :ocr-crop-mode="ocrCropMode"
          @action-start="loading = true"
          @action-end="loading = false"
          @refresh-needed="handleRefreshNeeded"
        />
      </el-aside>

      <el-aside width="350px" class="right-pane">
        <StepBuilder 
          :env-id="envId"
          @refresh-needed="handleRefreshNeeded"
          @request-ocr-crop="handleRequestOcrCrop"
        >
          <template #header-actions>
            <el-dropdown 
              split-button 
              type="primary" 
              @click="handleRun" 
              @command="handleRunCommand"
              :disabled="!currentCase.id || isRunning"
              style="margin-right: 12px"
            >
              运行
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="multi">选择多设备运行</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <el-button 
              :icon="Upload" 
              type="success" 
              @click="caseStore.saveCase" 
              :loading="loading"
            >
              保存
            </el-button>
          </template>
        </StepBuilder>
      </el-aside>
    </el-container>

    <!-- 多设备运行弹窗 -->
    <el-dialog
      v-model="runDialogVisible"
      title="选择多设备执行"
      width="400px"
    >
      <el-form :model="multiRunForm" label-width="100px">
        <el-form-item label="设备列表">
          <el-select
            v-model="multiRunForm.deviceSerials"
            placeholder="请选择执行设备"
            multiple
            collapse-tags
            collapse-tags-tooltip
            style="width: 100%"
          >
            <el-option
              v-for="d in deviceStageRef?.connectedDevices || []"
              :key="d.serial"
              :label="d.custom_name || d.market_name || d.model || d.serial"
              :value="d.serial"
              :disabled="d.status !== 'IDLE'"
            >
              <div style="display: flex; justify-content: space-between; align-items: center; width: 100%;">
                <span>{{ d.custom_name || d.market_name || d.model || d.serial }}</span>
                <el-tag :type="statusTagType(d.status)" size="small">{{ statusLabel(d.status) }}</el-tag>
              </div>
            </el-option>
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="runDialogVisible = false">取消</el-button>
          <el-button type="primary" @click="submitMultiRun">确定执行</el-button>
        </span>
      </template>
    </el-dialog>
  </el-container>
</template>

<style scoped>
.main-layout {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #f2f3f5;
  overflow: hidden;
}

/* app-header removed */

.header-left {
    display: flex;
    align-items: center;
    gap: 10px;
}

.back-btn {
    color: #606266;
    font-size: 20px;
}
.back-btn:hover {
    color: #409eff;
}

.logo {
  font-weight: bold;
  color: #303133;
  display: flex;
  align-items: center;
}

.title-input {
  width: 130px;
  font-size: 14px;
  font-weight: bold;
}

.title-input :deep(.el-input__wrapper) {
  background-color: transparent !important;
  box-shadow: none !important;
  padding-left: 0;
}

.title-input :deep(.el-input__inner) {
  color: #303133;
  font-weight: bold;
}

.title-input :deep(.el-input__wrapper:hover),
.title-input :deep(.el-input__wrapper.is-focus) {
  box-shadow: none !important;
  background-color: rgba(0, 0, 0, 0.05) !important;
}

.header-center {
  flex: 1;
}

.content-container {
  flex: 1;
  overflow: hidden;
  background: #f2f3f5;
  padding: 10px;
  gap: 10px;
}

.right-pane {
  overflow: hidden;
  height: 100%;
  background: #fff;
  border-radius: 4px;
  box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.1);
}

.general-pane {
    overflow: hidden;
    height: 100%;
    background: #fff;
    border-radius: 4px;
    box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.1); 
}

.center-pane {
  padding: 0;
  overflow: hidden;
  background: transparent;
}

.center-wrapper {
  display: flex;
  flex-direction: column;
  height: 100%;
  gap: 10px;
}

.center-wrapper > :first-child {
  flex: 1;
  min-height: 0;
}
</style>
