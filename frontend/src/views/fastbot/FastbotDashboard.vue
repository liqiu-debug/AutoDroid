<script setup>
import { ref, reactive, computed, onActivated, onDeactivated, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CaretRight } from '@element-plus/icons-vue'
import api from '@/api'

const router = useRouter()

// ---- 配置表单 ----
const form = reactive({
    package_name: 'com.ehaier.zgq.shop.mall',
    duration_min: 30,
    throttle: 500,
    enable_performance_monitor: true,
    enable_jank_frame_monitor: false,
    enable_local_replay: true,
    fault_tolerance: 'abort',
    capture_log: 'on',
    event_weight_mode: 'auto',
    pct_touch: 40,
    pct_motion: 30,
    pct_syskeys: 5,
    pct_majornav: 15,
})

const pctSum = computed(() => form.pct_touch + form.pct_motion + form.pct_syskeys + form.pct_majornav)
const pctRemaining = computed(() => Math.max(0, 100 - pctSum.value))

const buildPayload = () => ({
    package_name: form.package_name,
    duration: form.duration_min * 60,
    throttle: form.throttle,
    enable_performance_monitor: form.enable_performance_monitor,
    enable_jank_frame_monitor: form.enable_jank_frame_monitor,
    enable_local_replay: form.enable_local_replay,
    ignore_crashes: form.fault_tolerance !== 'abort',
    capture_log: form.capture_log === 'on',
    enable_custom_event_weights: form.event_weight_mode === 'custom',
    pct_touch: form.pct_touch,
    pct_motion: form.pct_motion,
    pct_syskeys: form.pct_syskeys,
    pct_majornav: form.pct_majornav,
})

// ---- 设备选择弹窗 ----
const deviceDialogVisible = ref(false)
const devices = ref([])
const selectedDevices = ref([])
const devicesLoading = ref(false)
const submitting = ref(false)

// ---- 最近一次任务状态 ----
const latestTask = ref(null)
let pollTimer = null
let pageActive = false

const fetchLatestTask = async () => {
    try {
        const res = await api.getFastbotTasks({ limit: 1 })
        const list = res.data || []
        latestTask.value = list.length > 0 ? list[0] : null
    } catch { /* ignore */ }
}

const fetchDevices = async () => {
    devicesLoading.value = true
    try {
        const res = await api.getFastbotDevices()
        devices.value = res.data || []
    } catch (err) {
        ElMessage.error('获取设备列表失败')
    } finally {
        devicesLoading.value = false
    }
}

const handleRunClick = () => {
    if (!form.package_name) {
        return ElMessage.warning('请输入目标包名')
    }
    selectedDevices.value = []
    fetchDevices()
    deviceDialogVisible.value = true
}

const handleConfirmRun = async () => {
    if (selectedDevices.value.length === 0) {
        return ElMessage.warning('请至少选择一台设备')
    }
    deviceDialogVisible.value = false
    submitting.value = true
    try {
        const promises = selectedDevices.value.map(serial => 
            api.runFastbot({
                ...buildPayload(),
                device_serial: serial,
            })
        )
        await Promise.all(promises)
        
        ElMessage.success(`已在 ${selectedDevices.value.length} 台设备上提交跑测任务`)
        await fetchLatestTask()
    } catch (err) {
        ElMessage.error('运行失败: ' + (err.response?.data?.detail || err.message))
    } finally {
        submitting.value = false
    }
}

/** 状态标签类型映射 */
const statusTagType = (status) => {
  const map = { IDLE: 'success', FASTBOT_RUNNING: 'danger', BUSY: 'danger', OFFLINE: 'info' }
  return map[status] || 'info'
}

/** 状态中文映射 */
const statusLabel = (status) => {
  const map = { IDLE: '🟢 空闲', FASTBOT_RUNNING: '🔴 跑测中', BUSY: '🔴 执行中', OFFLINE: '⚫ 离线' }
  return map[status] || status
}

const goToReports = () => {
    router.push({ path: '/execution/reports', query: { tab: 'fastbot' } })
}

const startPolling = () => {
    if (pollTimer) return
    pollTimer = setInterval(fetchLatestTask, 15000)
}

const stopPolling = () => {
    if (!pollTimer) return
    clearInterval(pollTimer)
    pollTimer = null
}

const activatePage = () => {
    if (pageActive) return
    pageActive = true
    startPolling()
}

const deactivatePage = () => {
    if (!pageActive) return
    pageActive = false
    stopPolling()
}

onMounted(() => {
    fetchLatestTask()
    activatePage()
})

onActivated(() => {
    activatePage()
})

onDeactivated(() => {
    deactivatePage()
})

onUnmounted(() => {
    deactivatePage()
})
</script>

<template>
    <div class="fastbot-container">
        <div class="content-wrapper">
            <!-- 配置面板 -->
            <el-card shadow="never" class="config-card">
                <template #header>
                    <div class="card-header">
                        <span class="card-title">智能探索配置</span>
                        <el-button
                            type="primary"
                            class="run-btn"
                            :icon="CaretRight"
                            :loading="submitting"
                            @click="handleRunClick"
                        >
                            立即运行
                        </el-button>
                    </div>
                </template>
                <div class="config-body">
                    <div class="config-form">
                        <el-form label-width="90px" label-position="left">
                            <div class="form-section-title">基础设置</div>
                            <el-form-item label="目标包名">
                                <el-input
                                    v-model="form.package_name"
                                    placeholder="com.example.app"
                                    clearable
                                    style="max-width: 360px"
                                />
                            </el-form-item>
                            <div class="form-row">
                                <el-form-item label="探索时长">
                                    <el-input-number
                                        v-model="form.duration_min"
                                        :min="1"
                                        :max="120"
                                        :step="5"
                                        style="width: 140px"
                                    />
                                    <span class="unit-text">分钟</span>
                                </el-form-item>
                                <el-form-item label="操作频率">
                                    <el-slider
                                        v-model="form.throttle"
                                        :min="0"
                                        :max="1000"
                                        :step="50"
                                        show-input
                                        style="width: 280px"
                                    />
                                    <span class="unit-text">ms</span>
                                </el-form-item>
                            </div>
                            <div class="monitor-options">
                                <el-form-item label="性能监控">
                                    <div class="monitor-option">
                                        <el-switch v-model="form.enable_performance_monitor" />
                                        <span class="monitor-tip">开启后采集 CPU、内存等性能指标</span>
                                    </div>
                                </el-form-item>
                                <el-form-item label="卡顿帧监控">
                                    <div class="monitor-option">
                                        <el-switch v-model="form.enable_jank_frame_monitor" />
                                        <span class="monitor-tip">开启后采集卡顿率，并在严重卡顿时自动录制 Perfetto Trace</span>
                                    </div>
                                </el-form-item>
                                <el-form-item label="异常回放" class="local-replay-item">
                                    <div class="monitor-option">
                                        <el-switch v-model="form.enable_local_replay" />
                                        <span class="monitor-tip">始终保留最近 30 秒画面，Crash / ANR 时自动导出本地回放</span>
                                    </div>
                                </el-form-item>
                            </div>

                            <div class="form-section-title" style="margin-top: 16px;">高级选项</div>

                            <div class="options-row">
                                <el-form-item label="容错策略">
                                    <el-select v-model="form.fault_tolerance" style="width: 170px">
                                        <el-option label="崩溃时立即停止" value="abort" />
                                        <el-option label="忽略崩溃继续探索" value="ignore" />
                                    </el-select>
                                </el-form-item>
                                <el-form-item label="抓取日志">
                                    <el-select v-model="form.capture_log" style="width: 170px">
                                        <el-option label="崩溃时截取 500 行" value="on" />
                                        <el-option label="不抓取" value="off" />
                                    </el-select>
                                </el-form-item>
                                <el-form-item label="事件配比">
                                    <el-select v-model="form.event_weight_mode" style="width: 170px">
                                        <el-option label="智能默认" value="auto" />
                                        <el-option label="自定义配比" value="custom" />
                                    </el-select>
                                </el-form-item>
                            </div>

                            <div v-if="form.event_weight_mode === 'custom'" class="event-weights">
                                <div class="form-row">
                                    <el-form-item label="点击">
                                        <el-slider v-model="form.pct_touch" :min="0" :max="100" show-input style="width: 280px" />
                                        <span class="unit-text">%</span>
                                    </el-form-item>
                                    <el-form-item label="滑动">
                                        <el-slider v-model="form.pct_motion" :min="0" :max="100" show-input style="width: 280px" />
                                        <span class="unit-text">%</span>
                                    </el-form-item>
                                </div>
                                <div class="form-row">
                                    <el-form-item label="系统键">
                                        <el-slider v-model="form.pct_syskeys" :min="0" :max="100" show-input style="width: 280px" />
                                        <span class="unit-text">%</span>
                                    </el-form-item>
                                    <el-form-item label="导航键">
                                        <el-slider v-model="form.pct_majornav" :min="0" :max="100" show-input style="width: 280px" />
                                        <span class="unit-text">%</span>
                                    </el-form-item>
                                </div>
                                <div class="pct-summary">
                                    <span>已分配: <b>{{ pctSum }}%</b></span>
                                    <span>其他随机事件: <b>{{ pctRemaining }}%</b></span>
                                    <span v-if="pctSum > 100" class="pct-warn">总和超过 100%，系统将自动归一化</span>
                                </div>
                            </div>
                        </el-form>
                    </div>
                </div>
            </el-card>

            <!-- 运行状态提示 -->
            <el-card shadow="never" class="status-card" v-if="latestTask">
                <div class="status-row">
                    <div class="status-info">
                        <span class="status-label">最近任务:</span>
                        <span class="pkg-mono">{{ latestTask.package_name }}</span>
                        <el-tag
                            :type="latestTask.status === 'COMPLETED' ? 'success' : latestTask.status === 'RUNNING' ? '' : latestTask.status === 'FAILED' ? 'danger' : 'info'"
                            size="small"
                            effect="plain"
                        >
                            {{ latestTask.status }}
                        </el-tag>
                        <span v-if="latestTask.total_crashes > 0" class="crash-badge">Crash: {{ latestTask.total_crashes }}</span>
                        <span v-if="latestTask.total_anrs > 0" class="anr-badge">ANR: {{ latestTask.total_anrs }}</span>
                    </div>
                    <el-button link type="primary" @click="goToReports">前往报告中心 →</el-button>
                </div>
            </el-card>
        </div>

        <!-- 设备选择弹窗 -->
        <el-dialog v-model="deviceDialogVisible" title="选择设备" width="440px" destroy-on-close>
            <div v-loading="devicesLoading">
                <el-select v-model="selectedDevices" multiple collapse-tags placeholder="选择目标设备" style="width: 100%">
                    <el-option
                        v-for="d in devices"
                        :key="d.serial"
                        :label="d.custom_name || d.market_name || d.model || d.device_name || d.serial"
                        :value="d.serial"
                        :disabled="d.status !== 'IDLE'"
                    >
                        <div style="display: flex; justify-content: space-between; align-items: center; width: 100%;">
                            <span>{{ d.custom_name || d.market_name || d.model || d.device_name || d.serial }}</span>
                            <el-tag :type="statusTagType(d.status)" size="small">{{ statusLabel(d.status) }}</el-tag>
                        </div>
                    </el-option>
                </el-select>
                <div v-if="devices.length === 0 && !devicesLoading" class="no-device-tip">
                    暂无可用设备，请连接 USB 设备后重试
                </div>
            </div>
            <template #footer>
                <el-button @click="deviceDialogVisible = false">取消</el-button>
                <el-button type="primary" @click="handleConfirmRun" :disabled="selectedDevices.length === 0">确认运行</el-button>
            </template>
        </el-dialog>
    </div>
</template>

<style scoped>
.fastbot-container {
    height: 100%;
    display: flex;
    flex-direction: column;
    background: #f2f3f5;
}

.content-wrapper {
    flex: 1;
    padding: 10px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.config-card {
    border-radius: 4px;
}

.card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
}

.card-title {
    font-size: 15px;
    font-weight: 600;
    color: #303133;
}

.config-body {
    display: flex;
    gap: 30px;
    align-items: flex-start;
}

.config-form {
    flex: 1;
}

.form-row {
    display: flex;
    gap: 20px;
    flex-wrap: wrap;
}

.unit-text {
    margin-left: 8px;
    color: #909399;
    font-size: 13px;
}

.form-section-title {
    font-size: 13px;
    color: #909399;
    font-weight: 600;
    margin-bottom: 12px;
    padding-bottom: 6px;
    border-bottom: 1px solid #ebeef5;
}

.options-row {
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
}

.monitor-options {
    padding-top: 4px;
}

.monitor-option {
    display: flex;
    align-items: center;
    gap: 12px;
}

.local-replay-item :deep(.el-form-item__label) {
    white-space: nowrap;
}

.monitor-tip {
    color: #909399;
    font-size: 12px;
}

.run-btn {
    padding: 8px 15px;
    font-weight: 600;
    border-radius: 6px;
}

.status-card {
    border-radius: 4px;
}

.status-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.status-info {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 14px;
}

.status-label {
    color: #909399;
}

.pkg-mono {
    font-family: monospace;
    color: #303133;
}

.crash-badge {
    color: #F56C6C;
    font-weight: 600;
    font-size: 13px;
}

.anr-badge {
    color: #E6A23C;
    font-weight: 600;
    font-size: 13px;
}

.no-device-tip {
    text-align: center;
    color: #909399;
    padding: 20px 0;
    font-size: 13px;
}

.event-weights {
    padding: 4px 0 0;
}

.event-weights .form-row {
    gap: 72px;
}

.pct-summary {
    display: flex;
    gap: 20px;
    font-size: 12px;
    color: #909399;
    margin-top: 4px;
}

.pct-warn {
    color: #E6A23C;
    font-weight: 600;
}
</style>
