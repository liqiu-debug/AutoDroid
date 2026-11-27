<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { Upload, VideoPlay, Back, CircleClose } from '@element-plus/icons-vue'
import { useRoute, useRouter } from 'vue-router'
import DeviceStage from '@/components/DeviceStage.vue'
import StepBuilder from '@/components/StepBuilder.vue'
import LogConsole from '@/components/LogConsole.vue'
import GeneralStepsPanel from '@/components/GeneralStepsPanel.vue'
import { useCaseStore } from '@/stores/useCaseStore'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useUnsavedGuard } from '@/composables/useUnsavedGuard'
import { deviceStatusLabel as statusLabel, deviceStatusTagType as statusTagType } from '@/utils/statusMeta'
import api from '@/api'
import { submissionNotice } from '@/utils/uiRunPresentation'
import { submitExecution, pendingExecution, rememberWebsocketExecution, settlePendingExecution, UNKNOWN_LAUNCH_MESSAGE } from '@/utils/executionLaunch'
import { getErrorDetail } from '@/utils/errors'
import { createPollingLoop } from '@/utils/polling'

const route = useRoute()
const router = useRouter()
const caseStore = useCaseStore()
const { currentCase, loading, saving, hasUnsavedChanges } = storeToRefs(caseStore)

useUnsavedGuard(hasUnsavedChanges)

const logConsoleRef = ref(null)
const deviceStageRef = ref(null)
const isRunning = ref(false)
const runPhase = ref('')
const pendingLaunch = ref(null)
const wsOutcomeUnknown = ref(false)
const runBusy = computed(() => Boolean(runPhase.value) || saving.value)
const runLabel = computed(() => ({ saving: '保存中', prechecking: '预检中', submitting: '启动中' }[runPhase.value] || (pendingLaunch.value || wsOutcomeUnknown.value ? '结果待确认' : isRunning.value ? '终止' : (!currentCase.value.id || hasUnsavedChanges.value ? '保存并运行' : '运行'))))
const activeRun = ref(null)
const terminatingRun = ref(false)
const ACTIVE_RUN_POLL_INTERVAL_MS = 3000
const envId = ref(null)
const environments = ref([])
let executionViewGeneration = 0
let websocketCaseId = null
const isCurrentCaseEvent = data => websocketCaseId !== null
  && String(websocketCaseId) === String(currentCase.value.id)
  && (data?.case_id == null || String(data.case_id) === String(websocketCaseId))

const detachExecutionView = () => {
  executionViewGeneration += 1
  websocketCaseId = null
  logConsoleRef.value?.clear?.()
  stopActiveRunPolling()
  runPhase.value = ''
  pendingLaunch.value = null
  wsOutcomeUnknown.value = false
  activeRun.value = null
  isRunning.value = false
  terminatingRun.value = false
}

// 获取 DeviceStage 的 OCR 框选模式状态
const ocrCropMode = computed(() => {
  return deviceStageRef.value?.ocrCropMode || false
})

const activeImageCropStepUuid = computed(() => {
  return deviceStageRef.value?.activeImageCropStepUuid || ''
})

const recordMode = computed(() => {
  return deviceStageRef.value?.syncMode ?? true
})

// 投屏模式下交互/单步执行响应无需整图截图（画面由视频流承担），
// 远程弱链路时每步可省一张整图传输
const includeInteractionScreenshot = computed(() => {
  return !(deviceStageRef.value?.liveMode ?? false)
})

const initData = async () => {
    const generation = executionViewGeneration
    const id = route.params.id
    if (id) {
        await caseStore.loadCase(id)
    } else {
        const folderId = route.query.folder_id ? Number(route.query.folder_id) : null
        caseStore.newCase({ folder_id: folderId })
    }
    if (generation !== executionViewGeneration) return
    try {
        const { data } = await api.getEnvironments()
        if (generation !== executionViewGeneration) return
        environments.value = data
        if (data && data.length > 0 && !envId.value) {
            envId.value = data[0].id
        }
    } catch (error) {
        console.error('获取环境列表失败', error)
    }
    pendingLaunch.value = pendingExecution('case', currentCase.value.id)
    restoreActiveRun()
}

watch(() => route.params.id, () => {
    detachExecutionView()
    initData()
}, { flush: 'sync' })

const goBack = () => {
    router.push('/ui/cases')
}

const ensureSaved = async () => {
  if (!currentCase.value.id || hasUnsavedChanges.value) {
    runPhase.value = 'saving'
    if (!await caseStore.saveCase()) return false
    if (hasUnsavedChanges.value) {
      ElMessage.warning('保存期间内容发生变化，请再次保存并运行')
      return false
    }
  }
  return true
}

const handleRun = async (options = {}) => {
  const startStepIndex = Number.isInteger(options?.startStepIndex) && options.startStepIndex > 0 ? options.startStepIndex : 0
  if (startStepIndex === 0 && !runBusy.value && isRunning.value && activeRun.value && !activeRun.value.unknown) return terminateActiveRun()
  pendingLaunch.value = pendingLaunch.value || pendingExecution('case', currentCase.value.id)
  if (runBusy.value || terminatingRun.value) return
  if (pendingLaunch.value || wsOutcomeUnknown.value) {
    await restoreActiveRun()
    return ElMessage.warning(UNKNOWN_LAUNCH_MESSAGE)
  }
  if (isRunning.value) return startStepIndex > 0 ? undefined : terminateActiveRun()
  if (!currentCase.value.steps.length) return ElMessage.warning('用例没有步骤')
  const selectedEnvironment = envId.value
  const currentDevice = recordingDeviceSerial.value
  const generation = executionViewGeneration
  let submittedCaseId = currentCase.value.id
  let connected = false
  runPhase.value = 'saving'
  try {
    if (!await ensureSaved()) return
    if (generation !== executionViewGeneration) return
    const caseId = currentCase.value.id
    submittedCaseId = caseId
    await api.ensureCaseExecutionAllowed(caseId)
    const info = await api.getExecutionCapabilities()
    if (generation !== executionViewGeneration) return
    if (!(info.clientMode === true && info.execution_preflight_v1 === true) && currentDevice) {
      runPhase.value = 'prechecking'
      const check = await precheckCaseOnDevice(caseId, currentDevice, selectedEnvironment)
      if (generation !== executionViewGeneration) return
      if (!check.ok) return ElMessage.error(`运行前预检失败: ${check.reason}`)
    }
    runPhase.value = 'submitting'
    activeRun.value = { kind: 'case', target_id: caseId, unknown: true, device_serials: currentDevice ? [currentDevice] : [] }
    rememberWebsocketExecution('case', caseId, currentDevice ? [currentDevice] : [])
    websocketCaseId = caseId
    if (startStepIndex > 0) {
      logConsoleRef.value.connect(caseId, selectedEnvironment, currentDevice, { startStepIndex })
    } else {
      logConsoleRef.value.connect(caseId, selectedEnvironment, currentDevice)
    }
    connected = true
  } catch (err) {
    if (generation !== executionViewGeneration) return
    settlePendingExecution('case', submittedCaseId)
    isRunning.value = false
    activeRun.value = null
    ElMessage.error('启动失败: ' + getErrorDetail(err))
  } finally {
    if (!connected && generation === executionViewGeneration) runPhase.value = ''
  }
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

const precheckCaseOnDevice = async (caseId, serial, selectedEnvironment = envId.value) => {
  try {
    const { data } = await api.precheckTestCase(caseId, selectedEnvironment, serial)
    if (data?.ok) return { ok: true }
    return { ok: false, reason: summarizePrecheckFailure(data) }
  } catch (err) {
    const detail = err?.response?.data?.detail || err?.message || '请求失败'
    return { ok: false, reason: `预检接口调用失败: ${detail}` }
  }
}

const submitMultiRun = async () => {
  pendingLaunch.value = pendingLaunch.value || pendingExecution('case', currentCase.value.id)
  if (runBusy.value || (isRunning.value && !pendingLaunch.value) || wsOutcomeUnknown.value) return
  if (pendingLaunch.value && !pendingLaunch.value.retryable) return ElMessage.warning(UNKNOWN_LAUNCH_MESSAGE)
  if (!pendingLaunch.value && !multiRunForm.value.deviceSerials.length) return ElMessage.warning('请至少选择一台设备')
  const selectedEnvironment = envId.value
  const selectedDevices = [...multiRunForm.value.deviceSerials]
  const generation = executionViewGeneration
  runPhase.value = 'saving'
  try {
    if (!pendingLaunch.value && !await ensureSaved()) return
    if (generation !== executionViewGeneration) return
    const selection = pendingLaunch.value || { kind: 'case', targetId: currentCase.value.id, envId: selectedEnvironment, devices: selectedDevices }
    runPhase.value = 'submitting'
    const { data, launch } = await submitExecution(api, selection, pendingLaunch.value)
    if (generation !== executionViewGeneration) return
    const notice = submissionNotice(data, launch.devices)
    ElMessage[notice.warning ? 'warning' : 'success'](notice.text)
    activeRun.value = {
      kind: 'case', target_id: launch.targetId, batch_id: data?.batch_id,
      run_ids: data?.run_ids || data?.runs?.map(item => item.run_id).filter(Boolean) || [],
      device_serials: notice.devices,
    }
    pendingLaunch.value = null
    isRunning.value = true
    startActiveRunPolling()
    runDialogVisible.value = false
  } catch (err) {
    if (generation !== executionViewGeneration) return
    if (err.launchPending) {
      pendingLaunch.value = err.launchPending
      activeRun.value = { kind: 'case', target_id: err.launchPending.targetId, unknown: true, device_serials: err.launchPending.devices }
      startActiveRunPolling()
      ElMessage.warning(UNKNOWN_LAUNCH_MESSAGE)
    } else {
      pendingLaunch.value = null
      ElMessage.error('启动批量执行失败: ' + getErrorDetail(err))
    }
  } finally {
    if (generation === executionViewGeneration) runPhase.value = ''
  }
}

const openMultiRunDialog = async () => {
  if (pendingLaunch.value || wsOutcomeUnknown.value) return ElMessage.warning(UNKNOWN_LAUNCH_MESSAGE)
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
  if (command === 'multi' && !runBusy.value && !isRunning.value) {
    if (currentCase.value.steps.length === 0) {
      ElMessage.warning('用例没有步骤')
      return
    }
    await openMultiRunDialog()
  }
}

const handleRunComplete = (data) => {
  if (!isCurrentCaseEvent(data)) return
  settlePendingExecution('case', currentCase.value.id)
  runPhase.value = ''
  wsOutcomeUnknown.value = false
  isRunning.value = false
  activeRun.value = null
  stopActiveRunPolling()
  if (data.success) {
    ElMessage.success(`执行完成: ${data.passed} 通过`)
  } else if (data.status === 'ABORTED') {
    ElMessage.warning('执行已终止')
  } else {
    ElMessage.error(`执行失败: ${data.failed} 个步骤失败`)
  }
}

const handleRunError = (data) => {
  if (!isCurrentCaseEvent(data)) return
  runPhase.value = ''
  if (data?.outcomeUnknown) {
    const accepted = Boolean(activeRun.value?.run_ids?.length)
    wsOutcomeUnknown.value = !accepted
    if (activeRun.value) activeRun.value.unknown = !accepted
    startActiveRunPolling()
    ElMessage.warning(UNKNOWN_LAUNCH_MESSAGE)
    return
  }
  settlePendingExecution('case', currentCase.value.id)
  wsOutcomeUnknown.value = false
  isRunning.value = false
  activeRun.value = null
  stopActiveRunPolling()
  ElMessage.error(data?.message || '执行启动被拒绝，请检查日志')
}

const handleRunStart = (data) => {
  if (!isCurrentCaseEvent(data)) return
  runPhase.value = ''
  isRunning.value = true
  wsOutcomeUnknown.value = false
  rememberWebsocketExecution('case', currentCase.value.id, data.device_serial ? [data.device_serial] : [], true)
  pendingLaunch.value = null
  startActiveRunPolling()
  activeRun.value = {
    kind: 'case',
    target_id: currentCase.value.id,
    batch_id: data.batch_id,
    run_ids: data.run_id ? [data.run_id] : [],
    device_serials: data.device_serial ? [data.device_serial] : []
  }
}

const terminateActiveRun = async () => {
  if (!activeRun.value || terminatingRun.value) return
  const targetId = activeRun.value.target_id || currentCase.value.id
  const record = activeRun.value
  const generation = executionViewGeneration
  terminatingRun.value = true
  try {
    await api.cancelRun({
      kind: 'case',
      target_id: targetId,
      batch_id: record.batch_id || null,
      run_ids: record.run_ids || [],
      device_serials: record.device_serials || []
    })
    const unresolved = pendingExecution('case', targetId)
    if (!unresolved || unresolved.transport === 'websocket') {
      settlePendingExecution('case', targetId)
      if (generation !== executionViewGeneration) return
      pendingLaunch.value = null
    } else {
      if (generation !== executionViewGeneration) return
      pendingLaunch.value = unresolved
    }
    ElMessage.warning('已发送终止请求')
    logConsoleRef.value?.markAborted?.()
    isRunning.value = false
    activeRun.value = null
    stopActiveRunPolling()
    await deviceStageRef.value?.refreshDevices?.()
  } catch (err) {
    if (generation !== executionViewGeneration) return
    ElMessage.error('终止失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    if (generation === executionViewGeneration) terminatingRun.value = false
  }
}

const restoreActiveRun = async () => {
  if (!currentCase.value.id) return
  const targetId = currentCase.value.id
  const generation = executionViewGeneration
  try {
    const { data } = await api.getActiveRuns('case', targetId)
    if (generation !== executionViewGeneration || targetId !== currentCase.value.id) return
    const items = data?.items || []
    if (items.length === 0) {
      const retained = pendingLaunch.value || pendingExecution('case', currentCase.value.id)
      if (retained?.transport === 'websocket' && retained.accepted) {
        settlePendingExecution('case', currentCase.value.id)
        pendingLaunch.value = null
        wsOutcomeUnknown.value = false
        runPhase.value = ''
        isRunning.value = false
        activeRun.value = null
        stopActiveRunPolling()
      }
      return
    }
    activeRun.value = {
      kind: 'case',
      target_id: currentCase.value.id,
      batch_id: items[0]?.batch_id,
      run_ids: items.map(item => item.run_id).filter(Boolean),
      device_serials: items.map(item => item.device_serial).filter(Boolean)
    }
    isRunning.value = true
    wsOutcomeUnknown.value = false
    if (pendingExecution('case', currentCase.value.id)?.transport === 'websocket' || pendingLaunch.value?.transport === 'websocket') {
      settlePendingExecution('case', currentCase.value.id)
      pendingLaunch.value = null
    }
    runPhase.value = ''
    startActiveRunPolling()
  } catch {}
}

const pollActiveRun = async () => {
  if (!currentCase.value.id || !activeRun.value) return
  const targetId = currentCase.value.id
  const generation = executionViewGeneration
  try {
    const { data } = await api.getActiveRuns('case', targetId)
    if (generation !== executionViewGeneration || targetId !== currentCase.value.id) return
    if ((data?.items || []).length === 0) {
      if (activeRun.value.unknown) return
      if (pendingExecution('case', currentCase.value.id)?.transport === 'websocket' || pendingLaunch.value?.transport === 'websocket') {
        settlePendingExecution('case', currentCase.value.id)
        pendingLaunch.value = null
      }
      wsOutcomeUnknown.value = false
      runPhase.value = ''
      isRunning.value = false
      activeRun.value = null
      stopActiveRunPolling()
    } else if (activeRun.value.unknown) {
      await restoreActiveRun()
    }
  } catch {}
}

// 运行状态轮询：在途不重叠 + 页面隐藏即暂停（utils/polling.js 统一门闩）
const activeRunPollLoop = createPollingLoop({
  intervalMs: ACTIVE_RUN_POLL_INTERVAL_MS,
  tick: pollActiveRun
})

const startActiveRunPolling = () => {
  stopActiveRunPolling()
  activeRunPollLoop.start()
}

const stopActiveRunPolling = () => {
  activeRunPollLoop.stop()
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

const recordingDevices = computed(() => deviceStageRef.value?.recordingDevices || [])
const connectedRunDevices = computed(() => deviceStageRef.value?.connectedDevices || [])
const recordingDeviceSerial = computed({
  get: () => deviceStageRef.value?.selectedSerial || '',
  set: serial => deviceStageRef.value?.selectDevice(serial)
})
const isDeviceSelectable = (device) => device?.status === 'IDLE'

const deviceUnavailableReason = (device) => {
  if (!device) return ''
  if (device.status === 'WDA_DOWN') return 'WDA 未就绪'
  if (device.status === 'BUSY') return '设备正忙'
  if (device.status === 'FASTBOT_RUNNING') return '正在跑稳定性测试'
  return ''
}

const hasWdaDownDevice = computed(() => connectedRunDevices.value.some(d => d.status === 'WDA_DOWN'))

onMounted(() => {
    initData()
})

onUnmounted(() => {
  detachExecutionView()
})
</script>

<template>
  <el-container class="main-layout">
    <header class="editor-header ad-surface">
      <el-button :icon="Back" text aria-label="返回用例列表" @click="goBack" />
      <el-input v-model="currentCase.name" placeholder="请输入用例名称" class="title-input" :disabled="runBusy" aria-label="用例名称" />
      <span class="save-state" role="status">{{ saving ? '保存中…' : hasUnsavedChanges ? '未保存' : currentCase.id ? '已保存' : '新用例' }}</span>
      <div class="editor-run-controls">
        <el-select v-model="envId" placeholder="运行环境" class="environment-select" :disabled="runBusy" aria-label="运行环境">
          <el-option v-for="env in environments" :key="env.id" :label="env.name" :value="env.id" />
        </el-select>
        <el-select v-model="recordingDeviceSerial" placeholder="选择调试设备" class="editor-device-select" :disabled="runBusy" aria-label="调试设备">
          <el-option v-for="d in recordingDevices" :key="d.serial" :label="d.custom_name || d.market_name || d.model || d.serial" :value="d.serial" :disabled="!deviceStageRef?.canObserveDevice(d)" />
        </el-select>
        <el-button :icon="Upload" @click="caseStore.saveCase" :loading="saving" :disabled="runBusy && !saving">保存</el-button>
        <el-dropdown split-button :type="isRunning ? 'danger' : 'primary'" @click="handleRun" @command="handleRunCommand" :disabled="runBusy || terminatingRun" :icon="isRunning ? CircleClose : VideoPlay">
          {{ runLabel }}
          <template #dropdown><el-dropdown-menu><el-dropdown-item command="multi" :disabled="isRunning">选择多设备运行</el-dropdown-item></el-dropdown-menu></template>
        </el-dropdown>
      </div>
    </header>
    <el-alert v-if="pendingLaunch || wsOutcomeUnknown" :title="UNKNOWN_LAUNCH_MESSAGE" type="warning" :closable="false" show-icon>
      <el-button v-if="pendingLaunch?.retryable" :disabled="runBusy" @click="submitMultiRun">确认原启动结果</el-button>
      <el-button :disabled="runBusy" @click="restoreActiveRun">查看执行状态</el-button>
    </el-alert>
    <div class="content-container">
      <section class="center-pane">
        <DeviceStage ref="deviceStageRef" :env-id="envId" hide-device-select @update-loading="loading = $event">
          <template #left><span class="pane-title">设备画面</span></template>
        </DeviceStage>
      </section>
      <section class="actions-pane">
        <GeneralStepsPanel :loading="loading" :device-serial="recordingDeviceSerial" :ocr-crop-mode="ocrCropMode" :record-mode="recordMode" :include-screenshot="includeInteractionScreenshot" @action-start="loading = true" @action-end="loading = false" @refresh-needed="handleRefreshNeeded" />
      </section>
      <section class="right-pane">
        <StepBuilder :env-id="envId" :device-serial="recordingDeviceSerial" :active-image-crop-step-uuid="activeImageCropStepUuid" :include-screenshot="includeInteractionScreenshot" :is-running="isRunning || runBusy" @refresh-needed="handleRefreshNeeded" @request-ocr-crop="handleRequestOcrCrop" @request-image-crop="handleRequestImageCrop" @run-from-step="index => handleRun({ startStepIndex: index })" />
      </section>
    </div>
    <LogConsole ref="logConsoleRef" :case-id="currentCase.id" @run-start="handleRunStart" @run-complete="handleRunComplete" @run-error="handleRunError" />

    <!-- 多设备运行弹窗 -->
    <el-dialog
      v-model="runDialogVisible"
      title="选择多设备执行"
      width="400px"
      :close-on-click-modal="!runBusy" :close-on-press-escape="!runBusy" :show-close="!runBusy"
    >
      <el-form v-loading="runDialogLoading" :model="multiRunForm" label-width="100px">
        <el-form-item label="设备列表">
          <el-select
            v-model="multiRunForm.deviceSerials"
            placeholder="请选择执行设备"
            multiple
            collapse-tags
            collapse-tags-tooltip
            :disabled="runDialogLoading || runBusy"
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
                  <span v-if="deviceUnavailableReason(d)" style="font-size: 12px; color: var(--ad-warning);">
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
          <el-button :disabled="runBusy" @click="runDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="runDialogLoading || runBusy" :disabled="runDialogLoading || runBusy" @click="submitMultiRun">{{ runPhase ? runLabel : '确定执行' }}</el-button>
        </span>
      </template>
    </el-dialog>
  </el-container>
</template>

<style scoped>
.main-layout { height: 100%; min-height: 0; padding: 16px; gap: 12px; background: var(--ad-bg); display: flex; flex-direction: column; overflow: hidden; }
.editor-header { display: flex; align-items: center; gap: 8px; padding: 8px 12px; flex-shrink: 0; border: 1px solid var(--ad-border); border-radius: var(--ad-panel-radius); background: var(--ad-surface); }
.title-input { flex: 1; min-width: 120px; max-width: 320px; font-size: 16px; font-weight: 600; }
.title-input :deep(.el-input__wrapper) { box-shadow: none; background: transparent; }
.save-state { color: var(--ad-muted); font-size: 12px; white-space: nowrap; }
.editor-run-controls { display: flex; align-items: center; gap: 8px; margin-left: auto; }
.environment-select { width: 116px; }
.editor-device-select { width: 170px; }
.content-container { display: grid; grid-template-columns: minmax(360px, 1fr) 220px 350px; gap: 12px; flex: 1; min-height: 0; overflow: hidden; }
.center-pane, .actions-pane, .right-pane { min-width: 0; min-height: 0; overflow: hidden; border: 1px solid var(--ad-border); border-radius: var(--ad-panel-radius); background: var(--ad-surface); }
.center-pane { display: flex; }
.center-pane > * { flex: 1; min-width: 0; }
/* The general-steps list is an authoring surface, so it stays a column; the
   panel scrolls on its own when the viewport is short. */
.actions-pane { overflow-y: auto; }
.pane-title { font-size: 13px; color: var(--ad-text); font-weight: 600; }
.run-warning-hint { margin-top: 6px; font-size: 12px; color: var(--ad-warning); }
/* The case editor is a desktop-only route, so all three columns stay; the
   side panes shrink before the device view does. */
@media (max-width: 1400px) { .content-container { grid-template-columns: minmax(320px, 1fr) 200px 330px; } }
@media (max-width: 1180px) { .editor-header { flex-wrap: wrap; } .editor-run-controls { flex-wrap: wrap; } .content-container { grid-template-columns: minmax(280px, 1fr) 190px 310px; gap: 8px; } }
</style>
