<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import { connect, disconnect } from 'echarts/core'
import StatCard from './components/StatCard.vue'
import StartupReportSection from './components/StartupReportSection.vue'
import PerformanceChartCard from './components/PerformanceChartCard.vue'
import JankChartCard from './components/JankChartCard.vue'
import CrashEventsCard from './components/CrashEventsCard.vue'
import JankEventsCard from './components/JankEventsCard.vue'
import TraceArtifactsCard from './components/TraceArtifactsCard.vue'
import LogAnalysisDialog from './components/LogAnalysisDialog.vue'
import ReplayDialog from './components/ReplayDialog.vue'
import TraceAiDialog from './components/TraceAiDialog.vue'
import { requestTraceAiSummary } from './components/traceAi'
import {
    clockTimeToTimestamp,
    formatDurationSeconds,
    formatMetric,
    formatPercent,
    formatReplayStatus,
    formatTime,
    getTraceCaptureMode,
    getTraceFrameStats,
    isReplayReady,
    pickMedianMetric,
    resolveReportBaseDate,
} from './components/reportFormatters'

const route = useRoute()
const router = useRouter()

const taskId = Number(route.params.id)
const chartGroup = `fastbot-report-${taskId}`
const task = ref(null)
const report = ref(null)
const loading = ref(true)

const batchTraceAiLoading = ref(false)
const devicesMap = ref({})
const activeJankEventTime = ref('')

const logDialogRef = ref(null)
const replayDialogRef = ref(null)
const traceAiDialogRef = ref(null)
const jankEventsCardRef = ref(null)

const bindCharts = async () => {
    await nextTick()
    connect(chartGroup)
}

const fetchData = async () => {
    loading.value = true
    try {
        const [taskRes, reportRes, deviceRes] = await Promise.all([
            api.getFastbotTask(taskId),
            api.getFastbotReport(taskId),
            api.getDeviceList().catch(() => ({ data: [] }))
        ])
        task.value = taskRes.data
        report.value = reportRes.data

        const map = {}
        if (deviceRes.data) {
            deviceRes.data.forEach(d => {
                map[d.serial] = d
            })
        }
        devicesMap.value = map
    } catch (err) {
        ElMessage.error('获取报告数据失败')
    } finally {
        loading.value = false
    }
}

const formatDeviceName = (identifier) => {
    if (!identifier) return '未知设备'
    const dev = devicesMap.value[identifier]
    if (dev) {
        const namePart = dev.custom_name || dev.market_name || dev.model
        if (namePart) return namePart
    }
    // Strip trailing parenthesized serial from DB historical strings
    if (typeof identifier === 'string') {
        return identifier.replace(/\s*\([^)]+\)$/, '')
    }
    return identifier
}

const perfData = computed(() => report.value?.performance_data || [])
const jankData = computed(() => report.value?.jank_data || [])
const jankEvents = computed(() => report.value?.jank_events || [])
const traceArtifacts = computed(() => report.value?.trace_artifacts || [])
const crashEvents = computed(() => report.value?.crash_events || [])
const summary = computed(() => report.value?.summary || {})
const isStartupSession = computed(() => summary.value?.session_type === 'startup')
const isManualFluencySession = computed(() => summary.value?.session_type === 'fluency_manual')
const manualMarkers = computed(() => Array.isArray(summary.value?.manual_markers) ? summary.value.manual_markers : [])
const markerSegments = computed(() => Array.isArray(summary.value?.marker_segments) ? summary.value.marker_segments : [])
const verdict = computed(() => summary.value?.verdict || null)
const performanceMonitorEnabled = computed(() => summary.value?.performance_monitor_enabled !== false)
const jankFrameMonitorEnabled = computed(() => summary.value?.jank_frame_monitor_enabled === true)
const localReplayEnabled = computed(() => summary.value?.local_replay_enabled === true)
const reportTitle = computed(() => {
    if (isStartupSession.value) return `冷热启动报告 — ${task.value?.package_name || ''}`
    return `${performanceMonitorEnabled.value ? '性能报告' : '智能探索报告'} — ${task.value?.package_name || ''}`
})
const jankMonitoringMode = computed(() => {
    if (!jankFrameMonitorEnabled.value) return 'disabled'
    return summary.value?.jank_monitoring_mode || 'gfxinfo'
})
const jankMonitoringModeLabel = computed(() => {
    if (!jankFrameMonitorEnabled.value) return '已关闭'
    const mode = jankMonitoringMode.value
    if (mode === 'framestats+perfetto') return '逐帧采集 + Perfetto'
    if (mode === 'framestats') return '逐帧采集'
    if (mode === 'gfxinfo+perfetto') return '系统帧统计 + Perfetto'
    if (mode === 'gfxinfo') return '系统帧统计'
    return mode
})

const reportBaseDate = computed(() => resolveReportBaseDate(task.value?.started_at))

const traceFrameTimelineSummary = computed(() => {
    const analyzedArtifacts = traceArtifacts.value
        .filter(artifact => artifact?.analysis_status === 'ANALYZED')
    const preferredArtifacts = analyzedArtifacts.some(artifact => getTraceCaptureMode(artifact) === 'continuous')
        ? analyzedArtifacts.filter(artifact => getTraceCaptureMode(artifact) === 'continuous')
        : analyzedArtifacts
    const analyzed = preferredArtifacts
        .map(artifact => getTraceFrameStats(artifact))
        .filter(stats => Number(stats.cadence_fps) > 0 || Number(stats.effective_fps) > 0 || Number(stats.present_delay_p95_ms) > 0)

    if (analyzed.length === 0) return null

    return {
        cadenceFps: pickMedianMetric(analyzed.map(stats => stats.cadence_fps)),
        effectiveFps: pickMedianMetric(analyzed.map(stats => stats.effective_fps)),
        p95DelayMs: pickMedianMetric(analyzed.map(stats => stats.present_delay_p95_ms)),
    }
})

const traceSummaryScopeLabel = computed(() => {
    const analyzedArtifacts = traceArtifacts.value
        .filter(artifact => artifact?.analysis_status === 'ANALYZED')
    if (analyzedArtifacts.length === 0) return ''
    const hasContinuous = analyzedArtifacts.some(artifact => getTraceCaptureMode(artifact) === 'continuous')
    return hasContinuous ? '全程结论' : '末 30 秒结论'
})

const verdictTagType = computed(() => {
    const level = verdict.value?.level
    if (level === 'GOOD') return 'success'
    if (level === 'FAIR') return 'warning'
    if (level === 'POOR') return 'danger'
    return 'info'
})

const analyzedTraceArtifacts = computed(() => (
    traceArtifacts.value.filter(artifact => artifact?.analysis_status === 'ANALYZED')
))

const continuousTraceCount = computed(() => (
    traceArtifacts.value.filter(artifact => getTraceCaptureMode(artifact) === 'continuous').length
))

const diagnosticTraceCount = computed(() => (
    traceArtifacts.value.filter(artifact => getTraceCaptureMode(artifact) === 'diagnostic').length
))

const openLogDialog = (event) => {
    logDialogRef.value?.open(event)
}

const openReplayDialog = (event) => {
    if (!isReplayReady(event)) {
        ElMessage.warning(formatReplayStatus(event, localReplayEnabled.value))
        return
    }
    replayDialogRef.value?.open(event)
}

const openTraceAiDialog = (artifact) => {
    traceAiDialogRef.value?.open(artifact)
}

const generateAllTraceSummaries = async () => {
    await hydrateTraceSummaries()
}

const hydrateTraceSummaries = async (options = {}) => {
    const { silent = false } = options

    if (batchTraceAiLoading.value) return

    const targets = analyzedTraceArtifacts.value.filter(artifact => !artifact.ai_summary)
    if (targets.length === 0) {
        if (!silent) {
            ElMessage.success('当前已分析 Trace 都已有 AI 总结')
        }
        return
    }

    batchTraceAiLoading.value = true
    try {
        const results = await Promise.allSettled(
            targets.map(async (artifact) => {
                const data = await requestTraceAiSummary(taskId, artifact)
                if (data) {
                    artifact.ai_summary = data.analysis_result
                    artifact.ai_summary_cached = data.cached || false
                }
                return artifact.path
            }),
        )
        const successCount = results.filter(result => result.status === 'fulfilled').length
        const failCount = results.length - successCount
        if (!silent) {
            if (failCount > 0) {
                ElMessage.warning(`AI 总结已生成 ${successCount} 条，失败 ${failCount} 条`)
            } else {
                ElMessage.success(`已生成 ${successCount} 条 AI 总结`)
            }
        }
    } catch (err) {
        if (!silent) {
            const msg = err.response?.data?.detail || err.message || '批量生成 AI 总结失败'
            ElMessage.error(msg)
        }
    } finally {
        batchTraceAiLoading.value = false
    }
}

const focusJankEvent = (row) => {
    if (!row?.time) return
    activeJankEventTime.value = row.time
    jankEventsCardRef.value?.setCurrentRow?.(row)
}

const findClosestJankEvent = (targetTimestamp, maxDeltaMs = 5000) => {
    if (!Number.isFinite(targetTimestamp)) return null
    let bestRow = null
    let minDelta = Infinity
    jankEvents.value.forEach((row) => {
        const rowTimestamp = clockTimeToTimestamp(reportBaseDate.value, row.time)
        if (!Number.isFinite(rowTimestamp)) return
        const delta = Math.abs(rowTimestamp - targetTimestamp)
        if (delta < minDelta) {
            minDelta = delta
            bestRow = row
        }
    })
    return minDelta <= maxDeltaMs ? bestRow : null
}

const handleJankChartPointClick = (targetTimestamp) => {
    const matched = findClosestJankEvent(targetTimestamp)
    if (matched) {
        focusJankEvent(matched)
    }
}

const goBack = () => {
    router.push({ path: '/execution/reports', query: { tab: isStartupSession.value ? 'startup' : 'fastbot' } })
}

onMounted(() => {
    fetchData()
})

onUnmounted(() => {
    disconnect(chartGroup)
})

watch(
    () => [perfData.value.length, jankData.value.length, jankFrameMonitorEnabled.value, performanceMonitorEnabled.value],
    () => {
        bindCharts()
    },
)
</script>

<template>
    <div class="report-detail-container" v-loading="loading">
        <!-- 顶部导航栏 -->
        <div class="top-bar">
            <el-button text :icon="ArrowLeft" @click="goBack">返回列表</el-button>
            <span class="title" v-if="task">
                {{ reportTitle }}
            </span>
        </div>

        <div class="detail-body" v-if="task && report">
            <StartupReportSection
                v-if="isStartupSession"
                :task="task"
                :summary="summary"
                :trace-artifacts="traceArtifacts"
                :device-name="formatDeviceName(task.device_serial)"
                :analyzed-count="analyzedTraceArtifacts.length"
                :batch-loading="batchTraceAiLoading"
                @open-trace-ai="openTraceAiDialog"
                @generate-summaries="generateAllTraceSummaries"
            />

            <template v-else>
            <el-card
                v-if="jankFrameMonitorEnabled && verdict"
                shadow="never"
                class="verdict-card"
            >
                <div class="verdict-header">
                    <div>
                        <div class="verdict-title">诊断结论</div>
                        <div class="verdict-subtitle">先看这里，再决定是否进入 Trace 明细排查。</div>
                    </div>
                    <el-tag :type="verdictTagType" effect="dark" size="large">
                        流畅度评级：{{ verdict.label || '-' }}
                    </el-tag>
                </div>
                <div class="verdict-grid">
                    <div class="verdict-item">
                        <div class="verdict-label">主要判断</div>
                        <div class="verdict-text">{{ verdict.reason || '-' }}</div>
                    </div>
                    <div class="verdict-item">
                        <div class="verdict-label">建议动作</div>
                        <div class="verdict-text">{{ verdict.suggestion || '-' }}</div>
                    </div>
                </div>
            </el-card>

            <!-- 任务概要卡片 -->
            <div class="summary-cards">
                <StatCard label="状态">
                    <el-tag :type="task.status === 'COMPLETED' ? 'success' : 'danger'" effect="plain">{{ task.status }}</el-tag>
                </StatCard>
                <StatCard v-if="performanceMonitorEnabled" label="平均 CPU" tone="primary" :value="`${summary.avg_cpu || 0}%`" />
                <StatCard v-if="performanceMonitorEnabled" label="峰值 CPU" :value="`${summary.max_cpu || 0}%`" />
                <StatCard v-if="performanceMonitorEnabled" label="平均内存" tone="success" :value="`${summary.avg_mem || 0} MB`" />
                <StatCard v-if="performanceMonitorEnabled" label="峰值内存" :value="`${summary.max_mem || 0} MB`" />
                <StatCard label="崩溃次数" tone="danger" :value="summary.total_crashes || 0" />
                <StatCard label="ANR 次数" tone="warning" :value="summary.total_anrs || 0" />
            </div>

            <div v-if="jankFrameMonitorEnabled" class="summary-cards">
                <StatCard label="监控模式" tone="primary" :value="jankMonitoringModeLabel" />
                <StatCard label="全程 Trace" tone="primary" :value="continuousTraceCount" />
                <StatCard label="异常 Trace" tone="warning" :value="diagnosticTraceCount" />
                <StatCard label="活跃窗口平均卡顿率" tone="warning" :value="formatPercent(summary.active_avg_jank_rate)" />
                <StatCard label="最大卡顿率" tone="danger" :value="formatPercent(summary.max_jank_rate)" />
                <StatCard label="最差窗口时间点" tone="danger" :value="summary.peak_jank_rate_window?.time || '--'" />
                <StatCard label="严重卡顿次数" tone="danger" :value="summary.severe_jank_events || 0" />
                <StatCard label="已分析 Trace" tone="success" :value="summary.analyzed_trace_count || 0" />
                <StatCard
                    label="目标帧率"
                    tone="primary"
                    :value="traceFrameTimelineSummary ? formatMetric(traceFrameTimelineSummary.cadenceFps) : '--'"
                />
                <StatCard
                    label="实际流畅帧率"
                    tone="success"
                    :value="traceFrameTimelineSummary ? formatMetric(traceFrameTimelineSummary.effectiveFps) : '--'"
                />
                <StatCard
                    label="Trace P95 呈现延迟"
                    tone="warning"
                    :value="traceFrameTimelineSummary ? `${formatMetric(traceFrameTimelineSummary.p95DelayMs)} ms` : '--'"
                />
            </div>
            <div v-if="traceFrameTimelineSummary" class="trace-insight-hint">
                Trace 指标基于 FrameTimeline 去重后的显示帧和呈现延迟；当前顶部 FPS 结论按“{{ traceSummaryScopeLabel }}”展示。
            </div>
            <div v-else-if="jankFrameMonitorEnabled" class="trace-insight-hint">
                当前任务暂无可用的 FrameTimeline Trace，暂不展示 FPS 结论。下方曲线仅用于展示 gfxinfo 的卡顿触发信号。
            </div>
            <div v-if="jankFrameMonitorEnabled" class="trace-insight-hint">
                活跃窗口平均卡顿率仅统计持续渲染窗口，不包含空闲页面或静止界面。
            </div>

            <el-card
                v-if="isManualFluencySession"
                shadow="never"
                class="events-card"
            >
                <template #header>
                    <div class="trace-header">
                        <span class="card-title">手动录制片段</span>
                        <span class="trace-hint">按打点拆分页面片段，便于对照你的手动操作路径。</span>
                    </div>
                </template>
                <div v-if="markerSegments.length > 0" class="marker-segment-grid">
                    <div
                        v-for="segment in markerSegments"
                        :key="`${segment.start_time}-${segment.label}`"
                        class="marker-segment-card"
                    >
                        <div class="marker-segment-top">
                            <el-tag type="warning" effect="plain">{{ segment.label }}</el-tag>
                            <span class="marker-segment-duration">{{ formatDurationSeconds(segment.duration_sec) }}</span>
                        </div>
                        <div class="marker-segment-time">{{ segment.start_time }} - {{ segment.end_time }}</div>
                        <div v-if="segment.activity" class="marker-segment-activity">{{ segment.activity }}</div>
                    </div>
                </div>
                <el-empty v-else description="当前录制没有形成有效片段，可能只记录了单个打点。" />
                <div v-if="manualMarkers.length > 0" class="marker-chip-row">
                    <el-tag
                        v-for="item in manualMarkers"
                        :key="`${item.time}-${item.label}`"
                        effect="plain"
                        class="marker-chip"
                    >
                        {{ item.time }} · {{ item.label }}
                    </el-tag>
                </div>
            </el-card>

            <!-- 性能折线图 -->
            <PerformanceChartCard
                v-if="performanceMonitorEnabled"
                :perf-data="perfData"
                :crash-events="crashEvents"
                :started-at="task.started_at || ''"
                :chart-group="chartGroup"
                @mark-point-click="openLogDialog"
            />

            <JankChartCard
                v-if="jankFrameMonitorEnabled"
                :jank-data="jankData"
                :trace-artifacts="traceArtifacts"
                :active-jank-event-time="activeJankEventTime"
                :started-at="task.started_at || ''"
                :chart-group="chartGroup"
                @point-click="handleJankChartPointClick"
            />

            <!-- 异常事件列表 -->
            <CrashEventsCard
                v-if="crashEvents.length > 0"
                :crash-events="crashEvents"
                :local-replay-enabled="localReplayEnabled"
                @view-log="openLogDialog"
                @view-replay="openReplayDialog"
            />

            <JankEventsCard
                v-if="jankFrameMonitorEnabled"
                ref="jankEventsCardRef"
                :jank-events="jankEvents"
                :active-jank-event-time="activeJankEventTime"
                @row-click="focusJankEvent"
            />

            <TraceArtifactsCard
                v-if="jankFrameMonitorEnabled"
                :trace-artifacts="traceArtifacts"
                :analyzed-count="analyzedTraceArtifacts.length"
                :batch-loading="batchTraceAiLoading"
                @open-trace-ai="openTraceAiDialog"
                @generate-summaries="generateAllTraceSummaries"
            />

            <!-- 任务详情 -->
            <el-card shadow="never" class="info-card">
                <template #header>
                    <span class="card-title">任务信息</span>
                </template>
                <el-descriptions :column="3" border size="small">
                    <el-descriptions-item label="包名">{{ task.package_name }}</el-descriptions-item>
                    <el-descriptions-item label="设备">{{ formatDeviceName(task.device_serial) }}</el-descriptions-item>
                    <el-descriptions-item label="执行人">{{ task.executor_name || '-' }}</el-descriptions-item>
                    <el-descriptions-item label="探索时长">{{ task.duration }}s</el-descriptions-item>
                    <el-descriptions-item label="操作频率">{{ task.throttle }}ms</el-descriptions-item>
                    <el-descriptions-item label="忽略崩溃">{{ task.ignore_crashes ? '是' : '否' }}</el-descriptions-item>
                    <el-descriptions-item label="性能监控">{{ performanceMonitorEnabled ? '已开启' : '已关闭' }}</el-descriptions-item>
                    <el-descriptions-item label="卡顿帧监控">{{ jankFrameMonitorEnabled ? '已开启' : '已关闭' }}</el-descriptions-item>
                    <el-descriptions-item label="异常回放">{{ localReplayEnabled ? '已开启' : '未开启' }}</el-descriptions-item>
                    <el-descriptions-item label="卡顿数据源">{{ jankMonitoringModeLabel }}</el-descriptions-item>
                    <el-descriptions-item label="开始时间">{{ formatTime(task.started_at) }}</el-descriptions-item>
                    <el-descriptions-item label="结束时间">{{ formatTime(task.finished_at) }}</el-descriptions-item>
                </el-descriptions>
            </el-card>
            </template>
        </div>

        <!-- 日志查看弹窗 (含 AI 分析) -->
        <LogAnalysisDialog
            ref="logDialogRef"
            :package-name="task?.package_name || ''"
            :device-serial="task?.device_serial || ''"
        />

        <ReplayDialog ref="replayDialogRef" :task-id="taskId" />

        <TraceAiDialog ref="traceAiDialogRef" :task-id="taskId" />
    </div>
</template>

<style scoped>
.report-detail-container {
    height: 100%;
    background: #f2f3f5;
    overflow-y: auto;
    overflow-x: hidden;
}

.top-bar {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 12px 20px;
    background: #fff;
    border-bottom: 1px solid #ebeef5;
    position: sticky;
    top: 0;
    z-index: 10;
}

.title {
    font-size: 15px;
    font-weight: 600;
    color: #303133;
}

.detail-body {
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
}

.verdict-card {
    border: 1px solid #d9ecff;
    background: linear-gradient(135deg, #f8fbff 0%, #eef6ff 100%);
}

.verdict-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
}

.verdict-title {
    font-size: 16px;
    font-weight: 700;
    color: #303133;
}

.verdict-subtitle {
    margin-top: 4px;
    font-size: 12px;
    color: #909399;
}

.verdict-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 12px;
    margin-top: 14px;
}

.verdict-item {
    padding: 12px 14px;
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.9);
    border: 1px solid #e4ecf5;
}

.verdict-label {
    font-size: 12px;
    color: #909399;
    margin-bottom: 6px;
}

.verdict-text {
    font-size: 14px;
    line-height: 1.6;
    color: #303133;
}

.summary-cards {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
}

.events-card, .info-card {
    border-radius: 4px;
}

.trace-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
}

.trace-hint {
    color: #909399;
    font-size: 12px;
}

.marker-segment-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 12px;
}

.marker-segment-card {
    border: 1px solid #ebeef5;
    border-radius: 8px;
    padding: 14px;
    background: #fafafa;
}

.marker-segment-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 8px;
}

.marker-segment-duration {
    color: #606266;
    font-size: 12px;
    font-weight: 600;
}

.marker-segment-time {
    color: #303133;
    font-size: 13px;
    font-weight: 600;
}

.marker-segment-activity {
    margin-top: 8px;
    color: #909399;
    font-size: 12px;
    word-break: break-all;
}

.marker-chip-row {
    margin-top: 14px;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
}

.card-title {
    font-size: 14px;
    font-weight: 600;
    color: #303133;
}

.trace-insight-hint {
    font-size: 12px;
    color: #606266;
    padding: 0 4px;
}
</style>
