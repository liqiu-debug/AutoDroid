<script setup>
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { VideoPlay, Refresh } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import api from '@/api'
import { deviceNameFromRecord } from '@/utils/deviceName'
import { runStatusTagType as statusTagType, runStatusLabel, deviceStatusLabel, deviceStatusTagType } from '@/utils/statusMeta'
import { useClientApp } from '@/composables/useClientApp'
import { createPollingLoop } from '@/utils/polling'
import {
    STARTUP_PROTOCOL_VERSION, STARTUP_UPGRADE_HINT, startupSubmissionForSession,
    startupPlatformLabel, startupModeLabel, startupExecutorLabel, startupDeviceReason,
    startupReadyLocators, startupMetrics, startupTraceAvailable, startupMetricLabel,
    startupProgressText, startupCanViewReport, startupPercentileText, startupCounts,
    isStartupActive, mergeStartupTasks, startupThresholdMetric,
} from '@/utils/startup'

const router = useRouter()
const { isClientApp, detectClientApp } = useClientApp()
const form = reactive({
    platform: 'android', package_name: '', activity_name: '',
    cold_enabled: true, hot_enabled: true, iterations: 3, cooldown_sec: 3,
    capture_log: true, ready_enabled: false, locator_type: 'text', locator_value: '', ready_timeout_sec: 10,
    perfetto_enabled: false, threshold_metric: 'total_time_ms', cold_threshold_ms: 5000, hot_threshold_ms: 1500,
})
const devices = ref([])
const deviceRecords = ref([])
const selectedDevices = ref([])
const devicesLoading = ref(false)
const capabilityError = ref('')
const capabilitiesReady = ref(false)
const localStartupSupported = ref(false)
const tasks = ref([])
const localTasks = ref([])
const taskListError = ref('')
const tasksLoading = ref(false)
const tasksPage = ref(1)
const tasksPageSize = ref(20)
const tasksTotal = ref(0)
const submitting = ref(false)
const stoppingTasks = ref([])
const initialSessionKey = api.getExecutionSessionKey()
const submission = startupSubmissionForSession(initialSessionKey)
const uncertainSubmission = ref(submission.hasPending())
const pendingPayload = submission.peek()
if (pendingPayload) {
    Object.assign(form, {
        platform: pendingPayload.platform, package_name: pendingPayload.package_name,
        activity_name: pendingPayload.activity_name || '', iterations: pendingPayload.iterations,
        cooldown_sec: pendingPayload.cooldown_sec, capture_log: pendingPayload.capture_log,
        cold_enabled: pendingPayload.startup_modes.includes('cold'), hot_enabled: pendingPayload.startup_modes.includes('hot'),
        ready_enabled: pendingPayload.ready_check.enabled, locator_type: pendingPayload.ready_check.locator_type,
        locator_value: pendingPayload.ready_check.locator_value, ready_timeout_sec: pendingPayload.ready_check.timeout_sec,
        threshold_metric: pendingPayload.thresholds.metric, cold_threshold_ms: pendingPayload.thresholds.cold_ms,
        hot_threshold_ms: pendingPayload.thresholds.hot_ms, perfetto_enabled: pendingPayload.perfetto_slow_trace.enabled,
    })
    selectedDevices.value = pendingPayload.device_serials
}
const startupFormElement = ref(null)
const advancedOptions = ref(null)
let pageActive = false
let refreshPromise = null

const revealStartupError = async (message, label, advanced = false) => {
    if (advanced && advancedOptions.value) advancedOptions.value.open = true
    await nextTick()
    const fields = [...(startupFormElement.value?.querySelectorAll('.el-form-item') || [])]
    const field = fields.find(item => item.querySelector('.el-form-item__label')?.textContent.includes(label))
    field?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    field?.querySelector('input, textarea, button')?.focus({ preventScroll: true })
    ElMessage.warning(message)
}
const availableDevices = computed(() => devices.value.filter(device => String(device.platform || 'android').toLowerCase() === form.platform))
const selectedDeviceRecords = computed(() => devices.value.filter(device => selectedDevices.value.includes(device.serial)))
const readyLocators = computed(() => startupReadyLocators(selectedDeviceRecords.value))
const supportedMetrics = computed(() => startupMetrics(selectedDeviceRecords.value))
const traceAvailable = computed(() => form.platform === 'android' && startupTraceAvailable(selectedDeviceRecords.value))
const formLocked = computed(() => submitting.value || uncertainSubmission.value)
const startupModes = computed(() => [form.cold_enabled ? 'cold' : null, form.hot_enabled ? 'hot' : null].filter(Boolean))
const deviceReason = device => startupDeviceReason(device, { web: !isClientApp.value })
const applicationLabel = computed(() => form.platform === 'ios' ? 'Bundle ID' : '目标包名')
const metricOptions = computed(() => ['total_time_ms', 'automation_ms', 'ready_ms'].filter(metric =>
    (form.platform !== 'ios' || metric !== 'total_time_ms') && supportedMetrics.value.includes(metric)))
const resetPlatform = () => {
    selectedDevices.value = []
    form.package_name = ''
    form.activity_name = ''
    form.locator_value = ''
    form.locator_type = form.platform === 'ios' ? 'accessibility_id' : 'text'
    form.threshold_metric = form.platform === 'ios' ? 'automation_ms' : 'total_time_ms'
    form.perfetto_enabled = false
}
watch(() => form.platform, resetPlatform)
watch(readyLocators, values => {
    if (!values.includes(form.locator_type)) form.locator_type = values[0] || (form.platform === 'ios' ? 'accessibility_id' : 'text')
})
watch(traceAvailable, available => { if (!available) form.perfetto_enabled = false })
watch(() => form.ready_enabled, enabled => {
    if (!enabled && form.threshold_metric === 'ready_ms') form.threshold_metric = form.platform === 'ios' ? 'automation_ms' : 'total_time_ms'
})

const fetchDevices = async () => {
    devicesLoading.value = true
    capabilitiesReady.value = false
    localStartupSupported.value = false
    capabilityError.value = ''
    try {
        await detectClientApp()
        // A desktop engine must explicitly advertise local interception support.
        // Old engines proxy unknown paths to the server, so never infer support from a cloud response.
        if (isClientApp.value || (typeof window !== 'undefined' && window.desktop)) {
            const info = await api.getClientInfo()
            if (!(Number(info.data?.startup?.protocol_version) >= STARTUP_PROTOCOL_VERSION)) {
                throw new Error(STARTUP_UPGRADE_HINT)
            }
            localStartupSupported.value = info.data?.clientMode === true
        }
        const res = await api.getStartupDevices()
        if (!Array.isArray(res.data?.items)) throw new Error('执行节点返回的启动能力格式不受支持，请更新服务端与客户端')
        devices.value = res.data.items
        capabilitiesReady.value = true
        if (!uncertainSubmission.value) selectedDevices.value = selectedDevices.value.filter(serial => availableDevices.value.some(device => device.serial === serial && !deviceReason(device)))
        if (!selectedDevices.value.length) {
            const first = availableDevices.value.find(device => !deviceReason(device))
            if (first) selectedDevices.value = [first.serial]
        }
    } catch (err) {
        devices.value = []
        selectedDevices.value = []
        capabilityError.value = err.response?.status === 404
            ? '执行节点尚未支持启动测试，请更新服务端或桌面客户端'
            : err.response?.data?.detail || err.message || '获取启动测试能力失败'
    } finally {
        devicesLoading.value = false
    }
}
const fetchTasks = async () => {
    tasksLoading.value = true
    try {
        const res = await api.getStartupTasks({ skip: (tasksPage.value - 1) * tasksPageSize.value, limit: tasksPageSize.value })
        tasks.value = mergeStartupTasks(res.data.items || [], tasksPage.value === 1 ? localTasks.value : [])
        tasksTotal.value = Math.max(res.data.total || 0, tasks.value.length)
        taskListError.value = ''
    } catch (err) {
        taskListError.value = '暂时无法获取云端任务，设备上的采集与本地结果仍会保留'
    } finally {
        tasksLoading.value = false
    }
}
const fetchLocalTasks = async () => {
    // Device discovery depends on cloud availability, while the outbox can be
    // read offline after this process has validated the current login.
    if (!localStartupSupported.value) return
    try {
        const res = await api.getStartupLocalResults()
        localTasks.value = Array.isArray(res.data?.items) ? res.data.items : []
        if (tasksPage.value === 1) {
            tasks.value = mergeStartupTasks(tasks.value, localTasks.value)
            tasksTotal.value = Math.max(tasksTotal.value, tasks.value.length)
        }
    } catch { /* Cloud list still works when local persistence is unavailable. */ }
}
const fetchDeviceRecords = async () => {
    try { deviceRecords.value = (await api.getDeviceList({ timeout: 30000 })).data || [] } catch { /* task snapshots remain available */ }
}
const handleTasksPageChange = val => { tasksPage.value = val; fetchTasks() }
const handleTasksSizeChange = val => { tasksPageSize.value = val; tasksPage.value = 1; fetchTasks() }
const refreshAll = () => {
    if (refreshPromise) return refreshPromise
    refreshPromise = (async () => {
        await Promise.all([fetchDevices(), fetchTasks(), fetchDeviceRecords()])
        await fetchLocalTasks()
    })().finally(() => { refreshPromise = null })
    return refreshPromise
}
const buildPayload = () => ({
    platform: form.platform,
    package_name: form.package_name.trim(),
    activity_name: form.platform === 'android' ? form.activity_name.trim() || null : null,
    device_serials: [...selectedDevices.value],
    startup_modes: startupModes.value,
    iterations: form.iterations,
    cooldown_sec: form.cooldown_sec,
    capture_log: form.capture_log,
    ready_check: { enabled: form.ready_enabled, locator_type: form.locator_type, locator_value: form.locator_value.trim(), timeout_sec: form.ready_timeout_sec },
    thresholds: { metric: form.threshold_metric, cold_ms: form.cold_threshold_ms, hot_ms: form.hot_threshold_ms },
    perfetto_slow_trace: { enabled: form.platform === 'android' && traceAvailable.value && form.perfetto_enabled },
})
const submit = async () => {
    if (submitting.value) return
    if (api.getExecutionSessionKey() !== initialSessionKey) return ElMessage.warning('登录会话已变化，请重新打开启动测试页面并查看原任务状态')
    if (!capabilitiesReady.value) return ElMessage.warning(capabilityError.value || '请等待执行节点能力检查完成')
    if (!uncertainSubmission.value) {
        if (!form.package_name.trim()) return revealStartupError(`请输入${applicationLabel.value}`, applicationLabel.value)
        if (!startupModes.value.length) return revealStartupError('请至少选择一种启动模式', '启动模式')
        if (!selectedDevices.value.length) return revealStartupError('请至少选择一台可用设备', '测试设备')
        if (selectedDeviceRecords.value.length !== selectedDevices.value.length || selectedDeviceRecords.value.some(device => device.platform !== form.platform || deviceReason(device))) {
            return revealStartupError('所选设备不可用或平台不一致，请刷新设备列表', '测试设备')
        }
        if (!metricOptions.value.includes(form.threshold_metric)) return revealStartupError('所选执行节点不支持此计时指标', '性能阈值')
        if (form.ready_enabled && (!form.locator_value.trim() || !readyLocators.value.includes(form.locator_type))) {
            return revealStartupError('请填写各执行节点均支持的业务就绪定位条件', '业务就绪检查', true)
        }
        if (form.threshold_metric === 'ready_ms' && !form.ready_enabled) return revealStartupError('使用业务就绪耗时阈值时需要开启就绪检查', '业务就绪检查', true)
    }
    const payload = submission.prepare(buildPayload())
    submitting.value = true
    try {
        const response = await api.runStartupTest(payload)
        const accepted = Array.isArray(response.data) ? response.data : response.data?.tasks
        if (!Array.isArray(accepted) || accepted.length !== payload.device_serials.length
            || accepted.some(task => !Number.isInteger(task?.id) || task.id <= 0)
            || new Set(accepted.map(task => task.id)).size !== accepted.length) {
            throw new Error('平台尚未确认全部任务编号，请查看已有任务后确认本次提交结果')
        }
        submission.resolve()
        uncertainSubmission.value = false
        const stopped = accepted.filter(task => ['ABORTED', 'FAILED'].includes(task.status))
        if (stopped.length) ElMessage.warning(`本次已登记 ${accepted.length} 台设备；${stopped.length} 台未开始或已失败，请查看任务原因`)
        else ElMessage.success(`已提交 ${accepted.length} 台设备的冷热启动测试`)
        tasksPage.value = 1
        await fetchTasks()
    } catch (err) {
        uncertainSubmission.value = submission.reject(err)
        const detail = err.response?.data?.detail
        const message = typeof detail === 'string' ? detail : detail?.message || err.message || '提交启动测试失败'
        ElMessage.error(uncertainSubmission.value ? `${message}；执行结果尚未确认，点击“确认提交结果”将安全重试同一次请求` : message)
    } finally { submitting.value = false }
}
const stopTask = async task => {
    if (!isStartupActive(task) || task.cancel_requested || stoppingTasks.value.includes(task.id)) return
    stoppingTasks.value = [...stoppingTasks.value, task.id]
    try {
        const res = await api.stopStartupTask(task.id)
        Object.assign(task, res.data || {}, { cancel_requested: true })
        ElMessage.success('已请求取消，已完成的轮次会保留')
        await fetchTasks()
    } catch (err) { ElMessage.error(err.response?.data?.detail || err.message || '取消失败，请重试') }
    finally { stoppingTasks.value = stoppingTasks.value.filter(id => id !== task.id) }
}
const formatDateTime = value => value ? dayjs(value).format('MM-DD HH:mm:ss') : '—'
const formatShortDateTime = value => value ? dayjs(value).format('MM-DD HH:mm') : '—'
const formatDeviceName = serial => deviceNameFromRecord(serial, devices.value.find(item => item.serial === serial) || deviceRecords.value.find(item => item.serial === serial), undefined, '未知设备')
const formatModes = task => (task.summary?.startup_config?.startup_modes || []).map(startupModeLabel).join(' / ') || '—'
const formatModeTags = task => (task.summary?.startup_config?.startup_modes || []).map(mode => ({ cold: '冷启动', hot: '后台恢复' })[mode] || startupModeLabel(mode))
const splitPercentileText = (task, mode) => {
    const text = startupPercentileText(task, mode, startupThresholdMetric(task))
    const [value, note] = text.split(' · ')
    return { value, note: note || '' }
}
const getSlowCount = task => Number(task.summary?.slow_count || 0)
const goReport = taskId => router.push(`/special/fastbot/report/${taskId}`)
const goReportCenter = () => router.push({ path: '/execution/reports', query: { tab: 'startup' } })
const TASK_POLL_INTERVAL_MS = 8000
const taskPollLoop = createPollingLoop({ intervalMs: TASK_POLL_INTERVAL_MS, tick: fetchTasks })
const localPollLoop = createPollingLoop({ intervalMs: 3000, tick: fetchLocalTasks })
const activatePage = () => { if (!pageActive) { pageActive = true; taskPollLoop.start(); localPollLoop.start() } }
const deactivatePage = () => { if (pageActive) { pageActive = false; taskPollLoop.stop(); localPollLoop.stop() } }
onMounted(async () => { await refreshAll(); activatePage() })
onActivated(() => { refreshAll(); activatePage() })
onDeactivated(deactivatePage)
onUnmounted(deactivatePage)
</script>

<template>
    <div class="startup-page">
        <div class="content-wrapper">
            <el-card shadow="never" class="config-card">
                <template #header>
                    <div class="card-header">
                        <span class="card-title">冷热启动配置</span>
                        <div class="header-actions">
                            <el-button :icon="Refresh" :disabled="formLocked" @click="refreshAll">刷新</el-button>
                            <el-button type="primary" :icon="VideoPlay" :loading="submitting" :disabled="!capabilitiesReady || devicesLoading" @click="submit">{{ uncertainSubmission ? '确认提交结果' : '开始测试' }}</el-button>
                        </div>
                    </div>
                </template>
                <el-alert v-if="capabilityError" :title="capabilityError" type="warning" :closable="false" class="collect-alert" />
                <el-alert v-if="uncertainSubmission" title="提交结果尚未确认" description="本次配置已锁定。点击“确认提交结果”会复用同一请求，不会重新创建任务。" type="warning" :closable="false" class="collect-alert" />
                <div ref="startupFormElement">
                    <el-form label-position="top" class="startup-form" :disabled="formLocked">
                        <div class="form-row-target" :class="{ 'is-ios': form.platform === 'ios' }">
                            <el-form-item label="平台">
                                <el-radio-group v-model="form.platform">
                                    <el-radio-button value="android">Android</el-radio-button>
                                    <el-radio-button value="ios">iOS</el-radio-button>
                                </el-radio-group>
                            </el-form-item>
                            <el-form-item :label="applicationLabel">
                                <el-input v-model="form.package_name" :placeholder="form.platform === 'ios' ? 'com.example.iosapp' : 'com.example.app'" clearable />
                            </el-form-item>
                            <el-form-item v-if="form.platform === 'android'" label="Activity">
                                <el-input v-model="form.activity_name" placeholder="留空自动解析 Launcher Activity" clearable />
                            </el-form-item>
                        </div>

                        <div class="form-row-execution">
                            <el-form-item label="测试设备">
                                <el-select
                                    v-model="selectedDevices"
                                    multiple
                                    filterable
                                    collapse-tags
                                    collapse-tags-tooltip
                                    popper-class="startup-device-popper"
                                    :loading="devicesLoading"
                                    :placeholder="`选择 ${startupPlatformLabel(form.platform)} 设备`"
                                    class="full-control"
                                >
                                    <el-option v-for="device in availableDevices" :key="device.serial" :label="formatDeviceName(device.serial)" :value="device.serial" :disabled="Boolean(deviceReason(device))" :title="deviceReason(device) || undefined">
                                        <div class="device-option">
                                            <div class="device-option-main">
                                                <span class="device-option-name">{{ formatDeviceName(device.serial) }}</span>
                                                <span v-if="deviceReason(device)" class="device-option-reason">{{ deviceReason(device) }}</span>
                                            </div>
                                            <el-tag :type="deviceStatusTagType(device.status)" size="small" class="device-option-status">{{ deviceStatusLabel(device.status) }}</el-tag>
                                        </div>
                                    </el-option>
                                </el-select>
                                <div v-if="!availableDevices.length && !devicesLoading" class="field-hint">暂无此平台设备，连接设备后刷新。</div>
                            </el-form-item>
                            <el-form-item label="启动模式">
                                <div class="mode-checks">
                                    <el-checkbox v-model="form.cold_enabled">冷启动</el-checkbox>
                                    <el-checkbox v-model="form.hot_enabled">热启动（后台恢复）</el-checkbox>
                                </div>
                            </el-form-item>
                            <el-form-item label="启动次数">
                                <div class="number-unit">
                                    <el-input-number v-model="form.iterations" :min="1" :max="100" class="compact-number" />
                                    <span class="unit-text">轮</span>
                                </div>
                            </el-form-item>
                            <el-form-item label="轮次间隔">
                                <div class="number-unit">
                                    <el-input-number v-model="form.cooldown_sec" :min="0" :max="60" class="compact-number" />
                                    <span class="unit-text">秒</span>
                                </div>
                            </el-form-item>
                        </div>

                        <el-form-item label="性能阈值" class="threshold-item">
                            <div class="threshold-row">
                                <el-select v-model="form.threshold_metric" class="metric-select">
                                    <el-option v-for="metric in metricOptions" :key="metric" :value="metric" :label="startupMetricLabel(metric, form.platform)" :disabled="metric === 'ready_ms' && !form.ready_enabled" />
                                </el-select>
                                <div class="threshold-pair">
                                    <span class="threshold-label">冷启动</span>
                                    <el-input-number v-model="form.cold_threshold_ms" :min="1" :max="120000" :step="100" class="threshold-number" />
                                    <span class="unit-text">ms</span>
                                </div>
                                <div class="threshold-pair">
                                    <span class="threshold-label">后台恢复</span>
                                    <el-input-number v-model="form.hot_threshold_ms" :min="1" :max="120000" :step="100" class="threshold-number" />
                                    <span class="unit-text">ms</span>
                                </div>
                            </div>
                        </el-form-item>

                        <p class="measurement-hint">冷启动保留应用数据；热启动测量应用仍在后台时恢复前台。准备、连接和固定等待不计入正式样本；每模式 / 每设备正式比较建议 20–30 轮。</p>

                        <details ref="advancedOptions" class="options-block">
                            <summary>高级配置 · 业务就绪、日志与取证</summary>
                            <div class="options-body">
                                <el-form-item label="业务就绪检查">
                                    <div class="ready-row">
                                        <el-switch v-model="form.ready_enabled" :disabled="!readyLocators.length" />
                                        <el-select v-model="form.locator_type" :disabled="!form.ready_enabled" class="locator-type-select">
                                            <el-option v-for="locator in readyLocators" :key="locator" :label="locator" :value="locator" />
                                        </el-select>
                                        <el-input v-model="form.locator_value" :disabled="!form.ready_enabled" placeholder="业务就绪关键元素 locator" class="locator-input" clearable />
                                        <div class="threshold-pair">
                                            <span class="threshold-label">超时</span>
                                            <el-input-number v-model="form.ready_timeout_sec" :disabled="!form.ready_enabled" :min="1" :max="60" class="compact-number" />
                                            <span class="unit-text">秒</span>
                                        </div>
                                    </div>
                                </el-form-item>
                                <div class="advanced-switches">
                                    <el-form-item label="抓取日志">
                                        <div class="switch-inline">
                                            <el-switch v-model="form.capture_log" />
                                            <span class="unit-text">保留轮次日志用于失败排查</span>
                                        </div>
                                    </el-form-item>
                                    <el-form-item v-if="form.platform === 'android'" label="慢启动取证">
                                        <div class="switch-inline">
                                            <el-switch v-model="form.perfetto_enabled" :disabled="!traceAvailable" />
                                            <span class="unit-text">{{ traceAvailable ? 'Perfetto 诊断复跑；性能阈值独立生效' : '所选执行节点暂不支持 Perfetto，仍可测量与判断性能' }}</span>
                                        </div>
                                    </el-form-item>
                                </div>
                            </div>
                        </details>
                    </el-form>
                </div>
            </el-card>
            <el-card shadow="never" class="tasks-card">
                <template #header>
                    <div class="card-header">
                        <span class="card-title">最近冷热启动任务</span>
                        <el-button link type="primary" @click="goReportCenter">查看报告中心</el-button>
                    </div>
                </template>
                <el-alert v-if="taskListError" :title="taskListError" type="warning" :closable="false" class="collect-alert" />
                <div class="tasks-table-area">
                    <el-table :data="tasks" v-loading="tasksLoading" class="tasks-table">
                        <el-table-column label="应用 / 平台" min-width="158">
                            <template #default="{ row }">
                                <span class="pkg-name ellipsis-line" :title="row.package_name">{{ row.package_name }}</span>
                                <small class="task-meta ellipsis-line">#{{ row.id }} · {{ startupPlatformLabel(row.platform || row.summary?.platform) }} · {{ startupExecutorLabel(row) }}</small>
                            </template>
                        </el-table-column>
                        <el-table-column label="设备" min-width="112" show-overflow-tooltip>
                            <template #default="{ row }">{{ formatDeviceName(row.device_serial) }}</template>
                        </el-table-column>
                        <el-table-column label="模式" width="84">
                            <template #default="{ row }">
                                <div class="mode-stack" :title="formatModes(row)">
                                    <span v-for="mode in formatModeTags(row)" :key="mode" class="mode-line">{{ mode }}</span>
                                    <span v-if="!formatModeTags(row).length" class="muted-text">—</span>
                                </div>
                            </template>
                        </el-table-column>
                        <el-table-column label="进度 / 有效" width="102">
                            <template #default="{ row }">
                                <span class="num-primary">{{ startupProgressText(row) }}</span>
                                <small class="task-meta">有效 {{ startupCounts(row).valid ?? '未校验' }} · 失败 {{ startupCounts(row).failed }}</small>
                            </template>
                        </el-table-column>
                        <el-table-column label="冷启动 P90" min-width="114">
                            <template #default="{ row }">
                                <div class="metric-cell">
                                    <span class="num-primary">{{ splitPercentileText(row, 'cold').value }}</span>
                                    <span v-if="splitPercentileText(row, 'cold').note" class="sample-warn">{{ splitPercentileText(row, 'cold').note }}</span>
                                </div>
                                <small class="task-meta ellipsis-line" :title="startupMetricLabel(startupThresholdMetric(row), row.platform || row.summary?.platform)">{{ startupMetricLabel(startupThresholdMetric(row), row.platform || row.summary?.platform) }}</small>
                            </template>
                        </el-table-column>
                        <el-table-column label="后台恢复 P90" min-width="96">
                            <template #default="{ row }">
                                <div class="metric-cell">
                                    <span class="num-primary">{{ splitPercentileText(row, 'hot').value }}</span>
                                </div>
                                <small v-if="splitPercentileText(row, 'hot').note" class="sample-warn">{{ splitPercentileText(row, 'hot').note }}</small>
                            </template>
                        </el-table-column>
                        <el-table-column label="超阈值" width="60" align="center">
                            <template #default="{ row }">
                                <span class="num-cell" :class="{ 'danger-text': getSlowCount(row) > 0 }">{{ getSlowCount(row) }}</span>
                            </template>
                        </el-table-column>
                        <el-table-column label="开始时间" width="104" align="center">
                            <template #default="{ row }">
                                <span class="num-cell" :title="formatDateTime(row.started_at || row.created_at)">{{ formatShortDateTime(row.started_at || row.created_at) }}</span>
                            </template>
                        </el-table-column>
                        <el-table-column label="状态" width="88" align="center">
                            <template #default="{ row }">
                                <el-tag :type="statusTagType(row.status)" effect="plain" size="small">{{ row.cancel_requested && isStartupActive(row) ? '取消中' : runStatusLabel(row.status) }}</el-tag>
                                <small v-if="row.sync_state === 'pending'" class="task-meta">待同步</small>
                                <small v-else-if="row.sync_state === 'superseded'" class="task-meta">归属变更</small>
                                <small v-if="row.error" class="task-meta ellipsis-line" :title="row.error">{{ row.error }}</small>
                            </template>
                        </el-table-column>
                        <el-table-column label="操作" width="98" align="center" fixed="right">
                            <template #default="{ row }">
                                <div class="row-actions">
                                    <el-button v-if="row.id && startupCanViewReport(row)" link type="primary" @click="goReport(row.id)">{{ isStartupActive(row) ? '部分报告' : '查看报告' }}</el-button>
                                    <el-button v-if="isStartupActive(row)" link type="danger" :loading="stoppingTasks.includes(row.id)" :disabled="row.cancel_requested" @click="stopTask(row)">取消</el-button>
                                </div>
                            </template>
                        </el-table-column>
                    </el-table>
                </div>
                <div v-if="tasksTotal > 0" class="pagination-footer">
                    <el-pagination v-model:current-page="tasksPage" v-model:page-size="tasksPageSize" :page-sizes="[10, 20, 50]" background layout="total, sizes, prev, pager, next" :total="tasksTotal" @size-change="handleTasksSizeChange" @current-change="handleTasksPageChange" />
                </div>
            </el-card>
        </div>
    </div>
</template>

<style scoped>
.startup-page {
    flex: 1;
    height: 0;
    overflow-y: auto;
    overflow-x: hidden;
    background: var(--ad-bg);
}

.content-wrapper {
    min-height: calc(100% - 32px);
    margin: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
}

.config-card,
.tasks-card {
    border-radius: var(--ad-radius);
}

.config-card {
    flex-shrink: 0;
}

.collect-alert {
    margin-bottom: 12px;
    border-radius: var(--ad-radius);
}

.tasks-card {
    flex: 1;
    display: flex;
    flex-direction: column;
}

.tasks-card :deep(.el-card__body) {
    display: flex;
    flex-direction: column;
}

.tasks-table-area {
    width: 100%;
    overflow-x: auto;
}

.tasks-table :deep(.el-table__cell) {
    padding: 7px 0;
    height: auto;
}

.tasks-table :deep(.cell) {
    line-height: 1.45;
}

.pagination-footer {
    flex-shrink: 0;
    padding-top: 12px;
    display: flex;
    justify-content: flex-end;
}

.card-header,
.header-actions,
.ready-row,
.threshold-row {
    display: flex;
    align-items: center;
    gap: 12px;
}

.card-header {
    justify-content: space-between;
}

.card-title {
    font-size: 15px;
    font-weight: 700;
    color: var(--ad-text);
}

.startup-form :deep(.el-form-item) {
    margin-bottom: 14px;
}

.startup-form :deep(.el-form-item__label) {
    padding-bottom: 4px;
    line-height: 18px;
    font-size: 12px;
    font-weight: 600;
    color: var(--ad-muted);
}

.form-row-target {
    display: grid;
    grid-template-columns: 152px minmax(220px, 1fr) minmax(220px, 1fr);
    gap: 0 16px;
    align-items: start;
}

.form-row-target.is-ios {
    grid-template-columns: 152px minmax(260px, 460px);
}

.form-row-execution {
    display: grid;
    grid-template-columns: minmax(260px, 1.45fr) auto 136px 136px;
    gap: 0 16px;
    align-items: start;
}

.full-control {
    width: 100%;
}

.mode-checks {
    display: flex;
    align-items: center;
    gap: 14px;
    min-height: 32px;
    white-space: nowrap;
}

.mode-checks :deep(.el-checkbox) {
    margin-right: 0;
}

.number-unit,
.threshold-pair,
.switch-inline {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-height: 32px;
}

.compact-number {
    width: 110px;
}

.threshold-item {
    margin-bottom: 10px;
}

.threshold-row {
    flex-wrap: wrap;
    gap: 10px 18px;
}

.metric-select {
    width: 224px;
}

.threshold-number {
    width: 128px;
}

.threshold-label {
    color: var(--ad-text);
    font-size: 12px;
    white-space: nowrap;
}

.measurement-hint {
    margin: 0 0 12px;
    padding: 7px 12px;
    border-radius: var(--ad-radius);
    background: var(--ad-bg);
    border: 1px solid var(--ad-border);
    color: var(--ad-muted);
    font-size: 12px;
    line-height: 1.55;
}

.field-hint {
    margin-top: 4px;
    color: var(--ad-muted);
    font-size: 12px;
    line-height: 1.4;
}

.options-block {
    padding-top: 10px;
    border-top: 1px solid var(--ad-border);
}

.options-block summary {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 8px;
    margin-left: -8px;
    border-radius: var(--ad-radius);
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
    color: var(--ad-text);
    list-style: none;
    user-select: none;
}

.options-block summary::-webkit-details-marker {
    display: none;
}

.options-block summary::before {
    content: '';
    width: 6px;
    height: 6px;
    border-right: 1.5px solid var(--ad-muted);
    border-bottom: 1.5px solid var(--ad-muted);
    transform: rotate(-45deg);
    transition: transform 0.15s ease;
    margin-right: 2px;
}

.options-block[open] summary::before {
    transform: rotate(45deg);
}

.options-block summary:hover {
    background: var(--ad-bg);
}

.options-body {
    padding-top: 12px;
}

.ready-row {
    width: 100%;
    flex-wrap: wrap;
    gap: 10px 12px;
}

.locator-type-select {
    width: 164px;
}

.locator-input {
    flex: 1;
    min-width: 220px;
    max-width: 440px;
}

.advanced-switches {
    display: flex;
    flex-wrap: wrap;
    gap: 0 36px;
    align-items: flex-start;
}

.advanced-switches :deep(.el-form-item) {
    margin-bottom: 4px;
}

.device-option {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    width: 100%;
    padding: 2px 0;
}

.device-option-main {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
}

.device-option-name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.device-option-reason {
    color: var(--ad-muted);
    font-size: 11px;
    line-height: 1.35;
    white-space: normal;
}

.device-option-status {
    flex-shrink: 0;
}

.unit-text,
.muted-text,
.task-meta {
    color: var(--ad-muted);
    font-size: 12px;
}

.task-meta {
    display: block;
    margin-top: 2px;
}

.ellipsis-line {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.pkg-name {
    font-family: Menlo, Monaco, Consolas, monospace;
    font-weight: 500;
    color: var(--ad-text);
}

.mode-stack {
    display: flex;
    flex-direction: column;
    gap: 1px;
}

.mode-line {
    font-size: 12px;
    color: var(--ad-text);
    white-space: nowrap;
}

.metric-cell {
    display: flex;
    align-items: baseline;
    gap: 5px;
    flex-wrap: wrap;
}

.num-primary {
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    color: var(--ad-text);
}

.num-cell {
    font-variant-numeric: tabular-nums;
}

.sample-warn {
    color: var(--ad-warning);
    font-size: 11px;
    white-space: nowrap;
}

.danger-text {
    color: var(--ad-danger);
    font-weight: 700;
}

.row-actions {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    white-space: nowrap;
}

.row-actions :deep(.el-button + .el-button) {
    margin-left: 0;
}

@media (max-width: 1120px) {
    .form-row-execution {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
}

@media (max-width: 767px) {
    .form-row-target,
    .form-row-target.is-ios,
    .form-row-execution {
        grid-template-columns: 1fr;
    }

    :deep(.el-button), :deep(.el-radio-button__inner), :deep(.el-select__wrapper), :deep(.el-collapse-item__header) { min-height: 44px; }
    :deep(.el-input__inner), :deep(.el-textarea__inner) { font-size: 16px; }
    :deep(.el-form-item__label), :deep(.el-table), :deep(.el-descriptions), :deep(.el-tabs__item), :deep(.el-collapse-item__content) { font-size: 14px; }
    :deep(.el-table__cell) { font-size: 14px; }
}
</style>
