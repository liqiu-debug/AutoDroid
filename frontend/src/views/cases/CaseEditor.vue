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

const activeImageCropStepUuid = computed(() => {
  return deviceStageRef.value?.activeImageCropStepUuid?.value || ''
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
  if (currentDevice) {
    const check = await precheckCaseOnDevice(currentCase.value.id, currentDevice)
    if (!check.ok) {
      ElMessage.error(`运行前预检失败: ${check.reason}`)
      return
    }
  }
  isRunning.value = true
  logConsoleRef.value?.connect(currentCase.value.id, envId.value, currentDevice)
}

const runDialogVisible = ref(false)
const runDialogLoading = ref(false)
const multiRunForm = ref({
  deviceSerials: []
})

const summarizePrecheckFailure = (payload) => {
  if (!payload || typeof payload !== 'object') return '预检失败'
  const globalFail = (payload.global_checks || []).find(item => item?.status === 'FAIL')
  if (globalFail) return globalFail.message || globalFail.code || '全局检查失败'

  const stepFail = (payload.steps || []).find(item => item?.status === 'FAIL')
  if (stepFail) return stepFail.message || stepFail.code || '步骤预检失败'

  if (!payload.has_runnable_steps) return '全部步骤将被跳过（当前设备无可执行步骤）'
  return '预检失败'
}

const precheckCaseOnDevice = async (caseId, serial) => {
  try {
    const { data } = await api.precheckTestCase(caseId, envId.value, serial)
    if (data?.ok) return { ok: true }
    return { ok: false, reason: summarizePrecheckFailure(data) }
  } catch (err) {
    const detail = err?.response?.data?.detail || err?.message || '请求失败'
    return { ok: false, reason: `预检接口调用失败: ${detail}` }
  }
}

const submitMultiRun = async () => {
  if (multiRunForm.value.deviceSerials.length === 0) {
    ElMessage.warning('请至少选择一台设备')
    return
  }
  try {
    const runnable = []
    const blocked = []
    for (const serial of multiRunForm.value.deviceSerials) {
      const check = await precheckCaseOnDevice(currentCase.value.id, serial)
      if (check.ok) runnable.push(serial)
      else blocked.push({ serial, reason: check.reason })
    }

    if (runnable.length === 0) {
      const first = blocked[0]
      ElMessage.error(`运行前预检未通过：${first ? `${first.serial} - ${first.reason}` : '无可执行设备'}`)
      return
    }

    const promises = runnable.map(serial =>
      api.runTestCaseAsync(currentCase.value.id, envId.value, serial)
    )
    await Promise.all(promises)
    if (blocked.length > 0) {
      const first = blocked[0]
      ElMessage.warning(`已在 ${runnable.length} 台设备启动；${blocked.length} 台预检失败（示例：${first.serial} - ${first.reason}）`)
    } else {
      ElMessage.success(`后台已开始在 ${promises.length} 台设备上执行用例`)
    }
    runDialogVisible.value = false
  } catch (err) {
    ElMessage.error('启动批量执行失败: ' + err.message)
  }
}

const openMultiRunDialog = async () => {
  multiRunForm.value.deviceSerials = []
  runDialogVisible.value = true
  runDialogLoading.value = true
  try {
    await deviceStageRef.value?.refreshDevices?.()
    multiRunForm.value.deviceSerials = deviceStageRef.value?.selectedSerial ? [deviceStageRef.value.selectedSerial] : []
  } finally {
    runDialogLoading.value = false
  }
}

const handleRunCommand = async (command) => {
  if (command === 'multi') {
    if (!currentCase.value.id) {
      ElMessage.warning('请先保存用例')
      return
    }
    if (currentCase.value.steps.length === 0) {
      ElMessage.warning('用例没有步骤')
      return
    }
    await openMultiRunDialog()
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

const handleRequestImageCrop = (step) => {
  if (deviceStageRef.value?.startImageCrop) {
    deviceStageRef.value.startImageCrop(step)
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

const connectedRunDevices = computed(() => deviceStageRef.value?.connectedDevices || [])
const recordingDeviceSerial = computed(() => {
  const selected = deviceStageRef.value?.selectedSerial
  if (!selected) return ''
  return typeof selected === 'string' ? selected : (selected.value || '')
})
const isDeviceSelectable = (device) => device?.status === 'IDLE'

const deviceUnavailableReason = (device) => {
  if (!device) return ''
  if (device.status === 'WDA_DOWN') return 'WDA 未就绪'
  if (device.status === 'BUSY') return '设备正忙'
  return ''
}

const hasWdaDownDevice = computed(() => connectedRunDevices.value.some(d => d.status === 'WDA_DOWN'))

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
          :device-serial="recordingDeviceSerial"
          :ocr-crop-mode="ocrCropMode"
          @action-start="loading = true"
          @action-end="loading = false"
          @refresh-needed="handleRefreshNeeded"
        />
      </el-aside>

      <el-aside width="350px" class="right-pane">
        <StepBuilder 
          :env-id="envId"
          :device-serial="recordingDeviceSerial"
          :active-image-crop-step-uuid="activeImageCropStepUuid"
          @refresh-needed="handleRefreshNeeded"
          @request-ocr-crop="handleRequestOcrCrop"
          @request-image-crop="handleRequestImageCrop"
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
      <el-form v-loading="runDialogLoading" :model="multiRunForm" label-width="100px">
        <el-form-item label="设备列表">
          <el-select
            v-model="multiRunForm.deviceSerials"
            placeholder="请选择执行设备"
            multiple
            collapse-tags
            collapse-tags-tooltip
            :disabled="runDialogLoading"
            style="width: 100%"
          >
            <el-option
              v-for="d in connectedRunDevices"
              :key="d.serial"
              :label="d.custom_name || d.market_name || d.model || d.serial"
              :value="d.serial"
              :disabled="!isDeviceSelectable(d)"
            >
              <div style="display: flex; justify-content: space-between; align-items: center; width: 100%;">
                <span>{{ d.custom_name || d.market_name || d.model || d.serial }}</span>
                <div style="display: flex; align-items: center; gap: 6px;">
                  <el-tag :type="statusTagType(d.status)" size="small">{{ statusLabel(d.status) }}</el-tag>
                  <span v-if="deviceUnavailableReason(d)" style="font-size: 12px; color: #e6a23c;">
                    {{ deviceUnavailableReason(d) }}
                  </span>
                </div>
              </div>
            </el-option>
          </el-select>
          <div v-if="hasWdaDownDevice" class="run-warning-hint">
            检测到 iOS 设备 WDA 异常，需在设备中心先执行“检测WDA”。
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="runDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="runDialogLoading" :disabled="runDialogLoading" @click="submitMultiRun">确定执行</el-button>
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

.run-warning-hint {
  margin-top: 6px;
  font-size: 12px;
  color: #e6a23c;
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
