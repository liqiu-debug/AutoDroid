<script setup>
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { VideoPlay, Refresh, View } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import api from '@/api'

const router = useRouter()

const form = reactive({
    package_name: 'com.ehaier.zgq.shop.mall',
    activity_name: '',
    cold_enabled: true,
    hot_enabled: true,
    iterations: 3,
    cooldown_sec: 3,
    capture_log: true,
    ready_enabled: false,
    locator_type: 'text',
    locator_value: '',
    ready_timeout_sec: 10,
    perfetto_enabled: true,
    cold_threshold_ms: 5000,
    hot_threshold_ms: 1500,
})

const devices = ref([])
const selectedDevices = ref([])
const devicesLoading = ref(false)
const tasks = ref([])
const tasksLoading = ref(false)
const tasksPage = ref(1)
const tasksPageSize = ref(20)
const tasksTotal = ref(0)
const submitting = ref(false)
let pollTimer = null
let pageActive = false

const availableDevices = computed(() => (
    (devices.value || []).filter(item => (
        String(item.platform || 'android').toLowerCase() === 'android'
        && String(item.status || '').toUpperCase() !== 'OFFLINE'
    ))
))

const startupModes = computed(() => {
    const modes = []
    if (form.cold_enabled) modes.push('cold')
    if (form.hot_enabled) modes.push('hot')
    return modes
})

const fetchDevices = async () => {
    devicesLoading.value = true
    try {
        const res = await api.getFastbotDevices()
        devices.value = res.data || []
        if (selectedDevices.value.length === 0 && availableDevices.value.length > 0) {
            selectedDevices.value = [availableDevices.value[0].serial]
        }
    } catch (err) {
        ElMessage.error('获取设备列表失败')
    } finally {
        devicesLoading.value = false
    }
}

const fetchTasks = async () => {
    tasksLoading.value = true
    try {
        const res = await api.getStartupTasks({
            skip: (tasksPage.value - 1) * tasksPageSize.value,
            limit: tasksPageSize.value,
        })
        tasks.value = res.data.items || []
        tasksTotal.value = res.data.total || 0
    } catch (err) {
        ElMessage.error('获取启动测试任务失败')
    } finally {
        tasksLoading.value = false
    }
}

const handleTasksPageChange = (val) => {
    tasksPage.value = val
    fetchTasks()
}

const handleTasksSizeChange = (val) => {
    tasksPageSize.value = val
    tasksPage.value = 1
    fetchTasks()
}

const refreshAll = async () => {
    await Promise.all([fetchDevices(), fetchTasks()])
}

const buildPayload = () => ({
    package_name: form.package_name.trim(),
    activity_name: form.activity_name.trim() || null,
    device_serials: selectedDevices.value,
    startup_modes: startupModes.value,
    iterations: form.iterations,
    cooldown_sec: form.cooldown_sec,
    capture_log: form.capture_log,
    ready_check: {
        enabled: form.ready_enabled,
        locator_type: form.locator_type,
        locator_value: form.locator_value.trim(),
        timeout_sec: form.ready_timeout_sec,
    },
    perfetto_slow_trace: {
        enabled: form.perfetto_enabled,
        cold_threshold_ms: form.cold_threshold_ms,
        hot_threshold_ms: form.hot_threshold_ms,
    },
})

const submit = async () => {
    if (!form.package_name.trim()) {
        ElMessage.warning('请输入目标包名')
        return
    }
    if (startupModes.value.length === 0) {
        ElMessage.warning('请至少选择一种启动模式')
        return
    }
    if (selectedDevices.value.length === 0) {
        ElMessage.warning('请至少选择一台设备')
        return
    }
    if (form.ready_enabled && !form.locator_value.trim()) {
        ElMessage.warning('开启首页就绪检查时需要填写 locator')
        return
    }

    submitting.value = true
    try {
        await api.runStartupTest(buildPayload())
        ElMessage.success(`已提交 ${selectedDevices.value.length} 台设备的冷热启动测试`)
        tasksPage.value = 1
        await fetchTasks()
    } catch (err) {
        ElMessage.error(err.response?.data?.detail || err.message || '提交启动测试失败')
    } finally {
        submitting.value = false
    }
}

const formatDateTime = (value) => value ? dayjs(value).format('MM-DD HH:mm:ss') : '-'
const formatMs = (value) => {
    const num = Number(value)
    return Number.isFinite(num) ? `${num} ms` : '-'
}
const formatDeviceName = (serial) => {
    const device = devices.value.find(item => item.serial === serial)
    return device?.custom_name || device?.market_name || device?.model || serial || '未知设备'
}
const statusTagType = (status) => {
    if (status === 'COMPLETED') return 'success'
    if (status === 'FAILED') return 'danger'
    if (status === 'RUNNING') return 'warning'
    return 'info'
}
const formatModes = (task) => {
    const modes = task.summary?.startup_config?.startup_modes || []
    if (!Array.isArray(modes) || modes.length === 0) return '-'
    return modes.map(mode => mode === 'cold' ? '冷启动' : '热启动').join(' / ')
}
const getAggregateValue = (task, mode, key) => task.summary?.startup_aggregate?.[mode]?.[key]
const getSlowCount = (task) => Number(task.summary?.slow_count || 0)
const getProgressText = (task) => {
    const summary = task.summary || {}
    const config = summary.startup_config || {}
    const expected = (Array.isArray(config.startup_modes) ? config.startup_modes.length : 0) * Number(config.iterations || 0)
    const finished = Number(summary.success_count || 0) + Number(summary.fail_count || 0)
    if (expected > 0) return `${finished}/${expected}`
    if (task.status === 'RUNNING' || task.status === 'PENDING') return '执行中'
    return '-'
}
const goReport = (taskId) => {
    router.push(`/special/fastbot/report/${taskId}`)
}
const goReportCenter = () => {
    router.push({ path: '/execution/reports', query: { tab: 'startup' } })
}

const startPolling = () => {
    if (pollTimer) return
    pollTimer = setInterval(fetchTasks, 8000)
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

onMounted(async () => {
    await refreshAll()
    activatePage()
})

onActivated(() => {
    refreshAll()
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
    <div class="startup-page">
        <div class="content-wrapper">
            <el-card shadow="never" class="config-card">
                <template #header>
                    <div class="card-header">
                        <span class="card-title">冷热启动配置</span>
                        <div class="header-actions">
                            <el-button :icon="Refresh" @click="refreshAll">刷新</el-button>
                            <el-button type="primary" :icon="VideoPlay" :loading="submitting" @click="submit">开始测试</el-button>
                        </div>
                    </div>
                </template>

                <el-form label-width="110px" label-position="left">
                    <div class="form-grid">
                        <el-form-item label="目标包名">
                            <el-input v-model="form.package_name" placeholder="com.example.app" clearable />
                        </el-form-item>
                        <el-form-item label="Activity">
                            <el-input v-model="form.activity_name" placeholder="留空自动解析 Launcher Activity" clearable />
                        </el-form-item>
                    </div>

                    <el-form-item label="测试设备">
                        <el-select
                            v-model="selectedDevices"
                            multiple
                            filterable
                            collapse-tags
                            collapse-tags-tooltip
                            :loading="devicesLoading"
                            placeholder="选择 Android 设备"
                            class="wide-control"
                        >
                            <el-option
                                v-for="device in availableDevices"
                                :key="device.serial"
                                :label="formatDeviceName(device.serial)"
                                :value="device.serial"
                                :disabled="String(device.status || '').includes('RUNNING') || device.status === 'BUSY'"
                            >
                                <span>{{ formatDeviceName(device.serial) }}</span>
                                <span class="device-serial">{{ device.serial }}</span>
                            </el-option>
                        </el-select>
                    </el-form-item>

                    <div class="form-grid">
                        <el-form-item label="启动模式">
                            <div class="inline-controls">
                                <el-checkbox v-model="form.cold_enabled">冷启动</el-checkbox>
                                <el-checkbox v-model="form.hot_enabled">热启动</el-checkbox>
                            </div>
                        </el-form-item>
                        <el-form-item label="启动次数">
                            <el-input-number v-model="form.iterations" :min="1" :max="100" />
                            <span class="unit-text">每模式/每设备</span>
                        </el-form-item>
                        <el-form-item label="轮次间隔">
                            <el-input-number v-model="form.cooldown_sec" :min="0" :max="60" />
                            <span class="unit-text">秒</span>
                        </el-form-item>
                    </div>

                    <div class="options-block">
                        <el-form-item label="慢启动取证">
                            <div class="inline-controls threshold-row">
                                <el-switch v-model="form.perfetto_enabled" />
                                <span class="unit-text">冷启动阈值</span>
                                <el-input-number v-model="form.cold_threshold_ms" :min="1" :max="120000" :step="100" />
                                <span class="unit-text">ms</span>
                                <span class="unit-text">热启动阈值</span>
                                <el-input-number v-model="form.hot_threshold_ms" :min="1" :max="120000" :step="100" />
                                <span class="unit-text">ms</span>
                            </div>
                        </el-form-item>
                        <el-form-item label="首页就绪检查">
                            <div class="ready-row">
                                <el-switch v-model="form.ready_enabled" />
                                <el-select v-model="form.locator_type" :disabled="!form.ready_enabled" style="width: 140px">
                                    <el-option label="text" value="text" />
                                    <el-option label="resource_id" value="resource_id" />
                                    <el-option label="description" value="description" />
                                    <el-option label="xpath" value="xpath" />
                                </el-select>
                                <el-input
                                    v-model="form.locator_value"
                                    :disabled="!form.ready_enabled"
                                    placeholder="首页关键元素 locator"
                                    class="locator-input"
                                    clearable
                                />
                                <span class="unit-text">首页就绪超时</span>
                                <el-input-number v-model="form.ready_timeout_sec" :disabled="!form.ready_enabled" :min="1" :max="60" />
                                <span class="unit-text">秒</span>
                            </div>
                        </el-form-item>
                        <el-form-item label="抓取日志">
                            <el-switch v-model="form.capture_log" />
                        </el-form-item>
                    </div>
                </el-form>
            </el-card>

            <el-card shadow="never" class="tasks-card">
                <template #header>
                    <div class="card-header">
                        <span class="card-title">最近冷热启动任务</span>
                        <el-button link type="primary" @click="goReportCenter">查看报告中心</el-button>
                    </div>
                </template>
                <div class="tasks-table-area">
                <el-table
                    :data="tasks"
                    v-loading="tasksLoading"
                    :header-cell-style="{ background: '#f5f7fa', color: '#606266' }"
                    height="100%"
                >
                    <el-table-column prop="id" label="ID" width="70" align="center" />
                    <el-table-column label="目标包名" min-width="190">
                        <template #default="{ row }"><span class="pkg-name">{{ row.package_name }}</span></template>
                    </el-table-column>
                    <el-table-column label="设备" width="170">
                        <template #default="{ row }">{{ formatDeviceName(row.device_serial) }}</template>
                    </el-table-column>
                    <el-table-column label="模式" width="130">
                        <template #default="{ row }">{{ formatModes(row) }}</template>
                    </el-table-column>
                    <el-table-column label="进度" width="90" align="center">
                        <template #default="{ row }">{{ getProgressText(row) }}</template>
                    </el-table-column>
                    <el-table-column label="冷启动就绪 P90" width="140" align="center">
                        <template #default="{ row }">{{ formatMs(getAggregateValue(row, 'cold', 'ready_p90_ms')) }}</template>
                    </el-table-column>
                    <el-table-column label="热启动就绪 P90" width="140" align="center">
                        <template #default="{ row }">{{ formatMs(getAggregateValue(row, 'hot', 'ready_p90_ms')) }}</template>
                    </el-table-column>
                    <el-table-column label="慢启动" width="90" align="center">
                        <template #default="{ row }">
                            <span :class="{ 'danger-text': getSlowCount(row) > 0 }">{{ getSlowCount(row) }}</span>
                        </template>
                    </el-table-column>
                    <el-table-column label="开始时间" width="140" align="center">
                        <template #default="{ row }">{{ formatDateTime(row.started_at || row.created_at) }}</template>
                    </el-table-column>
                    <el-table-column label="状态" width="100" align="center">
                        <template #default="{ row }">
                            <el-tag :type="statusTagType(row.status)" effect="plain" size="small">{{ row.status }}</el-tag>
                        </template>
                    </el-table-column>
                    <el-table-column label="操作" width="90" align="center" fixed="right">
                        <template #default="{ row }">
                            <el-button
                                v-if="row.report_ready"
                                :icon="View"
                                link
                                type="primary"
                                @click="goReport(row.id)"
                            />
                            <span v-else class="muted-text">等待报告</span>
                        </template>
                    </el-table-column>
                </el-table>
                </div>

                <div class="pagination-footer" v-if="tasksTotal > 0">
                    <el-pagination
                        v-model:current-page="tasksPage"
                        v-model:page-size="tasksPageSize"
                        :page-sizes="[10, 20, 50]"
                        :background="true"
                        layout="total, sizes, prev, pager, next"
                        :total="tasksTotal"
                        @size-change="handleTasksSizeChange"
                        @current-change="handleTasksPageChange"
                    />
                </div>
            </el-card>
        </div>
    </div>
</template>

<style scoped>
.startup-page {
    flex: 1;
    height: 0;
    overflow: hidden;
    background: #f2f3f5;
}

.content-wrapper {
    height: calc(100% - 20px);
    margin: 10px;
    display: flex;
    flex-direction: column;
    gap: 12px;
}

.config-card,
.tasks-card {
    border-radius: 4px;
}

.tasks-card {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
}

.tasks-card :deep(.el-card__body) {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
}

.tasks-table-area {
    flex: 1;
    min-height: 0;
}

.pagination-footer {
    flex-shrink: 0;
    padding-top: 10px;
    display: flex;
    justify-content: flex-end;
}

.card-header,
.header-actions,
.inline-controls,
.ready-row {
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
    color: #303133;
}

.form-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 0 20px;
}

.wide-control {
    width: 100%;
    max-width: 760px;
}

.options-block {
    padding-top: 8px;
    border-top: 1px solid #ebeef5;
}

.threshold-row {
    flex-wrap: wrap;
}

.ready-row {
    width: 100%;
    flex-wrap: wrap;
}

.locator-input {
    flex: 1;
    min-width: 240px;
    max-width: 520px;
}

.unit-text,
.device-serial,
.muted-text {
    color: #909399;
    font-size: 12px;
}

.device-serial {
    float: right;
}

.pkg-name {
    font-family: Menlo, Monaco, Consolas, monospace;
    color: #303133;
}

.danger-text {
    color: #F56C6C;
    font-weight: 700;
}
</style>
