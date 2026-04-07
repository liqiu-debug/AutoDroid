<script setup>
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { VideoPlay, VideoPause, Position, Timer } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import api from '@/api'

const router = useRouter()

const form = reactive({
    package_name: 'com.ehaier.zgq.shop.mall',
    device_serial: '',
    enable_performance_monitor: true,
    enable_jank_frame_monitor: true,
    capture_log: true,
    auto_launch_app: true,
})

const devices = ref([])
const devicesLoading = ref(false)
const sessions = ref([])
const sessionsLoading = ref(false)
const markerLabel = ref('')
const starting = ref(false)
const stopping = ref(false)
const marking = ref(false)
let pollTimer = null
let pageActive = false

const quickMarkers = [
    '首页',
    '列表页',
    '详情页',
    '下单页',
    '支付页',
    '弹窗场景',
]

const availableDevices = computed(() => (
    (devices.value || []).filter(item => (
        String(item.platform || 'android').toLowerCase() === 'android'
        && String(item.status || '').toUpperCase() !== 'OFFLINE'
    ))
))

const activeSession = computed(() => (
    sessions.value.find(item => item.status === 'RUNNING') || null
))

const recentSessions = computed(() => sessions.value)

const fetchDevices = async () => {
    devicesLoading.value = true
    try {
        const res = await api.getFastbotDevices()
        devices.value = res.data || []
        if (!form.device_serial && availableDevices.value.length > 0) {
            form.device_serial = availableDevices.value[0].serial
        }
    } catch (err) {
        ElMessage.error('获取设备列表失败')
    } finally {
        devicesLoading.value = false
    }
}

const fetchSessions = async () => {
    sessionsLoading.value = true
    try {
        const res = await api.getFluencySessions({ limit: 20 })
        sessions.value = res.data || []
    } catch (err) {
        ElMessage.error('获取录制会话失败')
    } finally {
        sessionsLoading.value = false
    }
}

const refreshAll = async () => {
    await Promise.all([fetchDevices(), fetchSessions()])
}

const startSession = async () => {
    if (!form.package_name.trim()) {
        ElMessage.warning('请输入目标包名')
        return
    }
    if (!form.device_serial) {
        ElMessage.warning('请选择设备')
        return
    }
    if (activeSession.value) {
        ElMessage.warning('当前已有进行中的录制会话')
        return
    }

    starting.value = true
    try {
        await api.startFluencySession({
            package_name: form.package_name.trim(),
            device_serial: form.device_serial,
            enable_performance_monitor: form.enable_performance_monitor,
            enable_jank_frame_monitor: form.enable_jank_frame_monitor,
            capture_log: form.capture_log,
            auto_launch_app: form.auto_launch_app,
        })
        ElMessage.success('已开始录制，请到手机上手动操作目标页面')
        await fetchSessions()
    } catch (err) {
        ElMessage.error(err.response?.data?.detail || err.message || '开始录制失败')
    } finally {
        starting.value = false
    }
}

const submitMarker = async (label = '') => {
    const session = activeSession.value
    const finalLabel = String(label || markerLabel.value || '').trim()
    if (!session) {
        ElMessage.warning('当前没有进行中的录制会话')
        return
    }
    if (!finalLabel) {
        ElMessage.warning('请输入打点标签')
        return
    }

    marking.value = true
    try {
        const res = await api.addFluencyMarker(session.task_id, { label: finalLabel })
        const updated = res.data
        sessions.value = sessions.value.map(item => item.task_id === updated.task_id ? updated : item)
        markerLabel.value = ''
        ElMessage.success(`已记录打点：${finalLabel}`)
    } catch (err) {
        ElMessage.error(err.response?.data?.detail || err.message || '记录打点失败')
    } finally {
        marking.value = false
    }
}

const stopSession = async () => {
    const session = activeSession.value
    if (!session) {
        ElMessage.warning('当前没有进行中的录制会话')
        return
    }

    stopping.value = true
    try {
        const res = await api.stopFluencySession(session.task_id)
        const updated = res.data
        sessions.value = [updated, ...sessions.value.filter(item => item.task_id !== updated.task_id)]
        ElMessage.success('录制已结束，报告已生成')
        router.push(`/special/fastbot/report/${updated.task_id}`)
    } catch (err) {
        ElMessage.error(err.response?.data?.detail || err.message || '结束录制失败')
    } finally {
        stopping.value = false
    }
}

const goToReport = (taskId) => {
    router.push(`/special/fastbot/report/${taskId}`)
}

const formatDateTime = (value) => value ? dayjs(value).format('MM-DD HH:mm:ss') : '-'
const formatPercent = (value) => `${((Number(value) || 0) * 100).toFixed(1)}%`
const formatStatus = (status) => {
    if (status === 'RUNNING') return '录制中'
    if (status === 'COMPLETED') return '已完成'
    if (status === 'FAILED') return '失败'
    return status || '-'
}
const statusTagType = (status) => {
    if (status === 'RUNNING') return 'danger'
    if (status === 'COMPLETED') return 'success'
    if (status === 'FAILED') return 'warning'
    return 'info'
}
const formatDeviceName = (serial) => {
    const device = devices.value.find(item => item.serial === serial)
    if (!device) return serial || '未知设备'
    return device.custom_name || device.market_name || device.model || device.serial
}

const startPolling = () => {
    if (pollTimer) return
    pollTimer = setInterval(fetchSessions, 8000)
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
    <div class="fluency-page">
        <div class="content-wrapper">
            <div class="hero-panel">
                <div>
                    <h1 class="hero-title">流畅度分析</h1>
                </div>
                <div class="hero-actions">
                    <el-button
                        type="primary"
                        :icon="VideoPlay"
                        :loading="starting"
                        :disabled="Boolean(activeSession)"
                        @click="startSession"
                    >
                        开始录制
                    </el-button>
                    <el-button
                        :icon="VideoPause"
                        :loading="stopping"
                        :disabled="!activeSession"
                        @click="stopSession"
                    >
                        结束录制
                    </el-button>
                </div>
            </div>

            <div class="fluency-grid">
                <el-card shadow="never" class="config-card">
                    <template #header>
                        <div class="section-head">
                            <span>录制配置</span>
                            <span class="section-tip">先选设备和包名，再开始录制</span>
                        </div>
                    </template>
                    <el-form label-position="top">
                        <el-form-item label="目标包名">
                            <el-input v-model="form.package_name" placeholder="com.example.app" clearable />
                        </el-form-item>
                        <el-form-item label="测试设备">
                            <el-select
                                v-model="form.device_serial"
                                placeholder="请选择设备"
                                filterable
                                style="width: 100%"
                                :loading="devicesLoading"
                            >
                                <el-option
                                    v-for="device in availableDevices"
                                    :key="device.serial"
                                    :label="device.custom_name || device.market_name || device.model || device.serial"
                                    :value="device.serial"
                                    :disabled="String(device.status || '').toUpperCase() !== 'IDLE'"
                                />
                            </el-select>
                            <div v-if="availableDevices.length === 0 && !devicesLoading" class="section-tip">
                                当前没有可用 Android 设备，请先连接设备或同步设备状态。
                            </div>
                        </el-form-item>
                        <div class="toggle-list">
                            <div class="toggle-row">
                                <div>
                                    <div class="toggle-title">性能采样</div>
                                    <div class="toggle-desc">录制 CPU 和内存曲线</div>
                                </div>
                                <el-switch v-model="form.enable_performance_monitor" />
                            </div>
                            <div class="toggle-row">
                                <div>
                                    <div class="toggle-title">卡顿帧监控</div>
                                    <div class="toggle-desc">录制 gfxinfo，并在严重卡顿时自动截取 Trace</div>
                                </div>
                                <el-switch v-model="form.enable_jank_frame_monitor" />
                            </div>
                            <div class="toggle-row">
                                <div>
                                    <div class="toggle-title">崩溃日志</div>
                                    <div class="toggle-desc">保留 CRASH/ANR 的日志快照</div>
                                </div>
                                <el-switch v-model="form.capture_log" />
                            </div>
                            <div class="toggle-row">
                                <div>
                                    <div class="toggle-title">自动拉起应用</div>
                                    <div class="toggle-desc">开始录制后自动打开目标 App</div>
                                </div>
                                <el-switch v-model="form.auto_launch_app" />
                            </div>
                        </div>
                    </el-form>
                </el-card>

                <el-card shadow="never" class="record-card">
                    <template #header>
                        <div class="section-head">
                            <span>录制面板</span>
                            <span class="section-tip">录制中可随时打点</span>
                        </div>
                    </template>
                    <div v-if="activeSession" class="session-live">
                        <div class="live-badge">
                            <span class="dot" />
                            正在录制
                        </div>
                        <div class="live-meta">
                            <div class="meta-item">
                                <div class="meta-label">设备</div>
                                <div class="meta-value">{{ formatDeviceName(activeSession.device_serial) }}</div>
                            </div>
                            <div class="meta-item">
                                <div class="meta-label">包名</div>
                                <div class="meta-value mono">{{ activeSession.package_name }}</div>
                            </div>
                            <div class="meta-item">
                                <div class="meta-label">开始时间</div>
                                <div class="meta-value">{{ formatDateTime(activeSession.started_at) }}</div>
                            </div>
                            <div class="meta-item">
                                <div class="meta-label">已打点数</div>
                                <div class="meta-value accent">{{ activeSession.marker_count }}</div>
                            </div>
                        </div>

                        <el-input
                            v-model="markerLabel"
                            placeholder="例如：详情页首屏、支付弹窗、切换 Tab"
                            class="marker-input"
                        >
                            <template #append>
                                <el-button :icon="Position" :loading="marking" @click="submitMarker()">记录打点</el-button>
                            </template>
                        </el-input>

                        <div class="quick-markers">
                            <span class="quick-label">快捷打点</span>
                            <el-button
                                v-for="item in quickMarkers"
                                :key="item"
                                text
                                class="quick-chip"
                                @click="submitMarker(item)"
                            >
                                {{ item }}
                            </el-button>
                        </div>

                        <div class="marker-list">
                            <div v-for="item in activeSession.markers" :key="`${item.time}-${item.label}`" class="marker-row">
                                <span class="marker-time">{{ item.time }}</span>
                                <span class="marker-name">{{ item.label }}</span>
                                <span v-if="item.activity" class="marker-activity">{{ item.activity }}</span>
                            </div>
                        </div>
                    </div>
                    <div v-else class="empty-record">
                        <el-icon class="empty-icon"><Timer /></el-icon>
                        <div class="empty-title">当前没有进行中的录制</div>
                        <div class="empty-desc">
                            点击“开始录制”后，直接去手机上手动操作你想测试的页面。
                            进入关键页面时打点，结束后就能按片段回看。
                        </div>
                    </div>
                </el-card>

                <el-card shadow="never" class="history-card" v-loading="sessionsLoading">
                    <template #header>
                        <div class="section-head">
                            <span>最近录制</span>
                            <span class="section-tip">手动录制历史和报告入口</span>
                        </div>
                    </template>
                    <el-table :data="recentSessions" :header-cell-style="{ background: '#f7f4ec', color: '#6b6254' }">
                        <el-table-column label="开始时间" min-width="150">
                            <template #default="{ row }">{{ formatDateTime(row.started_at || row.created_at) }}</template>
                        </el-table-column>
                        <el-table-column label="设备" min-width="160">
                            <template #default="{ row }">{{ formatDeviceName(row.device_serial) }}</template>
                        </el-table-column>
                        <el-table-column label="包名" min-width="220" show-overflow-tooltip>
                            <template #default="{ row }"><span class="mono">{{ row.package_name }}</span></template>
                        </el-table-column>
                        <el-table-column label="状态" width="110" align="center">
                            <template #default="{ row }">
                                <el-tag :type="statusTagType(row.status)" effect="plain">{{ formatStatus(row.status) }}</el-tag>
                            </template>
                        </el-table-column>
                        <el-table-column label="活跃卡顿率" width="120" align="center">
                            <template #default="{ row }">{{ formatPercent(row.summary?.active_avg_jank_rate) }}</template>
                        </el-table-column>
                        <el-table-column label="严重卡顿" width="100" align="center">
                            <template #default="{ row }">{{ row.summary?.severe_jank_events || 0 }}</template>
                        </el-table-column>
                        <el-table-column label="打点数" width="90" align="center" prop="marker_count" />
                        <el-table-column label="操作" width="140" align="center">
                            <template #default="{ row }">
                                <el-button
                                    link
                                    type="primary"
                                    :disabled="!row.report_ready"
                                    @click="goToReport(row.task_id)"
                                >
                                    查看报告
                                </el-button>
                            </template>
                        </el-table-column>
                    </el-table>
                </el-card>
            </div>
        </div>
    </div>
</template>

<style scoped>
.fluency-page {
    height: 100%;
    background: #f2f3f5;
}

.content-wrapper {
    height: 100%;
    padding: 10px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    overflow-y: auto;
    overflow-x: hidden;
    box-sizing: border-box;
}

.hero-panel {
    display: flex;
    justify-content: space-between;
    gap: 20px;
    align-items: flex-start;
    padding: 20px 24px;
    border-radius: 4px;
    background: #fff;
    border: 1px solid #ebeef5;
}

.hero-title {
    margin: 0;
    font-size: 24px;
    line-height: 1.2;
    color: #303133;
}

.hero-actions {
    display: flex;
    gap: 12px;
    flex-shrink: 0;
}

.fluency-grid {
    display: grid;
    grid-template-columns: 1.05fr 1fr;
    gap: 10px;
}

.config-card,
.record-card,
.history-card {
    border-radius: 4px;
}

.history-card {
    grid-column: 1 / -1;
}

.section-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
}

.section-tip {
    color: #909399;
    font-size: 12px;
}

.toggle-list {
    display: flex;
    flex-direction: column;
    gap: 14px;
}

.toggle-row {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    align-items: center;
    padding: 12px 14px;
    border-radius: 4px;
    background: #fafafa;
    border: 1px solid #f0f2f5;
}

.toggle-title {
    font-size: 14px;
    color: #303133;
    font-weight: 600;
}

.toggle-desc {
    font-size: 12px;
    color: #909399;
    margin-top: 4px;
}

.session-live {
    display: flex;
    flex-direction: column;
    gap: 16px;
}

.live-badge {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    width: fit-content;
    padding: 6px 12px;
    border-radius: 999px;
    background: #fff3f0;
    color: #f56c6c;
    font-weight: 600;
}

.dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #f56c6c;
}

.live-meta {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
}

.meta-item {
    padding: 12px 14px;
    border-radius: 4px;
    background: #fafafa;
    border: 1px solid #ebeef5;
}

.meta-label {
    font-size: 12px;
    color: #909399;
    margin-bottom: 6px;
}

.meta-value {
    font-size: 15px;
    color: #303133;
    font-weight: 600;
}

.meta-value.accent {
    color: #409EFF;
}

.marker-input {
    margin-top: 4px;
}

.quick-markers {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
}

.quick-label {
    font-size: 12px;
    color: #909399;
}

.quick-chip {
    color: #409EFF;
}

.marker-list {
    max-height: 260px;
    overflow: auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.marker-row {
    display: grid;
    grid-template-columns: 82px minmax(0, 1fr);
    gap: 10px;
    align-items: center;
    padding: 12px 14px;
    border-radius: 4px;
    background: #fafafa;
    border: 1px dashed #dcdfe6;
}

.marker-time,
.marker-activity,
.mono {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
}

.marker-time {
    color: #409EFF;
    font-weight: 600;
}

.marker-name {
    color: #303133;
    font-weight: 600;
}

.marker-activity {
    grid-column: 2;
    color: #909399;
    font-size: 12px;
    word-break: break-all;
}

.empty-record {
    min-height: 340px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    text-align: center;
    color: #909399;
    padding: 32px;
}

.empty-icon {
    font-size: 30px;
    margin-bottom: 14px;
    color: #409EFF;
}

.empty-title {
    font-size: 18px;
    color: #303133;
    font-weight: 700;
}

.empty-desc {
    max-width: 420px;
    line-height: 1.8;
    margin-top: 10px;
}

@media (max-width: 960px) {
    .hero-panel {
        flex-direction: column;
    }

    .fluency-grid {
        grid-template-columns: 1fr;
    }

    .history-card {
        grid-column: auto;
    }

    .hero-actions {
        width: 100%;
        flex-wrap: wrap;
    }

    .live-meta {
        grid-template-columns: 1fr;
    }
}
</style>
