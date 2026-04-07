<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, MagicStick, RefreshRight } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import FastbotReplayPlayer from '@/components/FastbotReplayPlayer.vue'
import dayjs from 'dayjs'
import VChart from 'vue-echarts'
import MarkdownIt from 'markdown-it'
import { use, connect, disconnect } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import {
    TitleComponent,
    TooltipComponent,
    LegendComponent,
    GridComponent,
    MarkPointComponent,
    ToolboxComponent,
    DataZoomComponent,
} from 'echarts/components'

use([
    CanvasRenderer,
    LineChart,
    TitleComponent,
    TooltipComponent,
    LegendComponent,
    GridComponent,
    MarkPointComponent,
    ToolboxComponent,
    DataZoomComponent,
])

// Markdown 渲染器
const md = new MarkdownIt({
    html: false,
    breaks: true,
    linkify: true,
})

const route = useRoute()
const router = useRouter()

const taskId = Number(route.params.id)
const chartGroup = `fastbot-report-${taskId}`
const task = ref(null)
const report = ref(null)
const loading = ref(true)

// 日志弹窗
const logDialogVisible = ref(false)
const logContent = ref('')
const logEventType = ref('')
const replayDialogVisible = ref(false)
const currentReplayEvent = ref(null)

// AI 分析状态
const aiAnalyzing = ref(false)
const aiResult = ref('')
const aiRenderedHtml = ref('')
const aiTokenUsage = ref(0)
const aiCached = ref(false)
const showAiResult = ref(false)

const traceAiDialogVisible = ref(false)
const currentTraceArtifact = ref(null)
const traceAiAnalyzing = ref(false)
const traceAiResult = ref('')
const traceAiRenderedHtml = ref('')
const traceAiTokenUsage = ref(0)
const traceAiCached = ref(false)
const batchTraceAiLoading = ref(false)

const devicesMap = ref({})
const jankEventTableRef = ref(null)
const activeJankEventTime = ref('')

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
const isManualFluencySession = computed(() => summary.value?.session_type === 'fluency_manual')
const manualMarkers = computed(() => Array.isArray(summary.value?.manual_markers) ? summary.value.manual_markers : [])
const markerSegments = computed(() => Array.isArray(summary.value?.marker_segments) ? summary.value.marker_segments : [])
const verdict = computed(() => summary.value?.verdict || null)
const performanceMonitorEnabled = computed(() => summary.value?.performance_monitor_enabled !== false)
const jankFrameMonitorEnabled = computed(() => summary.value?.jank_frame_monitor_enabled === true)
const localReplayEnabled = computed(() => summary.value?.local_replay_enabled === true)
const reportTitle = computed(() => `${performanceMonitorEnabled.value ? '性能报告' : '智能探索报告'} — ${task.value?.package_name || ''}`)
const currentReplayTitle = computed(() => {
    const event = currentReplayEvent.value
    if (!event) return '本地复现回放'
    return `${event.type} 本地复现回放 · ${event.time || '--'}`
})
const jankMonitoringMode = computed(() => {
    if (!jankFrameMonitorEnabled.value) return 'disabled'
    return summary.value?.jank_monitoring_mode || 'gfxinfo'
})
const jankMonitoringModeLabel = computed(() => {
    if (!jankFrameMonitorEnabled.value) return '已关闭'
    if (jankMonitoringMode.value === 'gfxinfo+perfetto') return '系统帧统计 + Perfetto'
    if (jankMonitoringMode.value === 'gfxinfo') return '系统帧统计'
    return jankMonitoringMode.value
})

const reportBaseDate = computed(() => {
    const startedAt = dayjs(task.value?.started_at)
    return startedAt.isValid() ? startedAt.startOf('day') : dayjs().startOf('day')
})

const clockTimeToTimestamp = (value, lastTimestamp = null) => {
    const timeText = String(value || '')
    const [hour, minute, second] = timeText.split(':').map(Number)
    if (![hour, minute, second].every(Number.isFinite)) return null

    let timestamp = reportBaseDate.value
        .hour(hour)
        .minute(minute)
        .second(second)
        .millisecond(0)
        .valueOf()

    if (lastTimestamp !== null && timestamp < lastTimestamp - (12 * 3600 * 1000)) {
        timestamp += 24 * 3600 * 1000
    }
    return timestamp
}

const formatAxisTime = (value) => dayjs(value).format('HH:mm:ss')

const buildClockSeries = (points, valueKey) => {
    let lastTimestamp = null
    return points
        .map((point) => {
            const timestamp = clockTimeToTimestamp(point.time, lastTimestamp)
            if (timestamp === null) return null
            lastTimestamp = timestamp
            return [timestamp, Number(point[valueKey])]
        })
        .filter((point) => point && Number.isFinite(point[1]))
}

const findClosestSeriesPoint = (series, targetTimestamp, maxDeltaMs = 5000) => {
    if (!Number.isFinite(targetTimestamp)) return null
    let bestPoint = null
    let minDelta = Infinity
    series.forEach((point) => {
        const delta = Math.abs(point[0] - targetTimestamp)
        if (delta < minDelta) {
            minDelta = delta
            bestPoint = point
        }
    })
    return minDelta <= maxDeltaMs ? bestPoint : null
}

const activeJankEventTimestamp = computed(() => clockTimeToTimestamp(activeJankEventTime.value))

const chartOption = computed(() => {
    const cpuSeries = buildClockSeries(perfData.value, 'cpu')
    const memSeries = buildClockSeries(perfData.value, 'mem')

    const crashMarkPoints = crashEvents.value
        .map((event) => {
            const eventTimestamp = clockTimeToTimestamp(event.time)
            const perfPoint = findClosestSeriesPoint(cpuSeries, eventTimestamp)
            if (eventTimestamp === null || !perfPoint) return null
            return {
                coord: [eventTimestamp, perfPoint[1]],
                itemStyle: { color: event.type === 'ANR' ? '#E6A23C' : '#F56C6C' },
                symbol: 'pin',
                symbolSize: 40,
                value: event.type,
                _eventData: event,
            }
        })
        .filter(Boolean)

    return {
        title: { text: '性能监控', left: 'center', textStyle: { fontSize: 15, color: '#303133' } },
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'cross' },
        },
        legend: { data: ['CPU (%)', '内存 (MB)'], top: 35 },
        toolbox: {
            right: 20,
            feature: { saveAsImage: {} },
        },
        grid: { left: 60, right: 60, top: 80, bottom: 60 },
        dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 10 }],
        xAxis: {
            type: 'time',
            boundaryGap: false,
            axisLabel: {
                formatter: (value) => formatAxisTime(value),
            },
        },
        yAxis: [
            {
                type: 'value',
                name: 'CPU (%)',
                position: 'left',
                axisLabel: { formatter: '{value}%' },
                min: 0,
            },
            {
                type: 'value',
                name: '内存 (MB)',
                position: 'right',
                axisLabel: { formatter: '{value} MB' },
                min: 0,
            },
        ],
        series: [
            {
                name: 'CPU (%)',
                type: 'line',
                smooth: true,
                data: cpuSeries,
                yAxisIndex: 0,
                lineStyle: { color: '#409EFF', width: 2 },
                itemStyle: { color: '#409EFF' },
                areaStyle: { color: 'rgba(64,158,255,0.08)' },
                markPoint: {
                    data: crashMarkPoints,
                    label: {
                        show: true,
                        formatter: (p) => p.data.value === 'ANR' ? 'ANR' : 'Crash',
                        color: '#fff',
                        fontSize: 10,
                    },
                },
            },
            {
                name: '内存 (MB)',
                type: 'line',
                smooth: true,
                data: memSeries,
                yAxisIndex: 1,
                lineStyle: { color: '#67C23A', width: 2 },
                itemStyle: { color: '#67C23A' },
                areaStyle: { color: 'rgba(103,194,58,0.08)' },
            },
        ],
    }
})

const formatPercent = (value) => `${((Number(value) || 0) * 100).toFixed(1)}%`
const formatMetric = (value, digits = 1) => {
    if (value === null || value === undefined || value === '') return '-'
    const num = Number(value)
    return Number.isFinite(num) ? num.toFixed(digits) : '-'
}
const pickMedianMetric = (values) => {
    const numbers = values
        .map(value => Number(value))
        .filter(value => Number.isFinite(value) && value > 0)
        .sort((a, b) => a - b)
    if (numbers.length === 0) return null
    const mid = Math.floor(numbers.length / 2)
    return numbers.length % 2 === 1
        ? numbers[mid]
        : (numbers[mid - 1] + numbers[mid]) / 2
}
const formatDiagnosisStatus = (value) => {
    const labelMap = {
        PENDING: '待分析',
        EXPORT_IN_PROGRESS: '录制中',
        ANALYZED: '已分析',
        EXPORT_FAILED: '导出失败',
        EXPORT_LIMIT_REACHED: '达到上限',
        EXPORT_COOLDOWN: '冷却中',
        ANALYSIS_FAILED: '分析失败',
        UNAVAILABLE: '未采集',
    }
    return labelMap[value] || value || '-'
}

const formatJankSeverity = (value) => {
    const labelMap = {
        CRITICAL: '严重卡顿',
        WARNING: '轻微卡顿',
    }
    return labelMap[value] || value || '-'
}

const formatJankReason = (value) => {
    const labelMap = {
        LOW_FPS: '帧率过低',
        HIGH_JANK_RATE: '卡顿帧占比高',
        FROZEN_FRAME: '画面冻结',
        TASK_COMPLETED: '任务结束后导出',
    }
    return labelMap[value] || value || '-'
}

const formatJankSource = (value) => {
    const labelMap = {
        gfxinfo: '系统帧统计',
        perfetto: 'Perfetto',
    }
    return labelMap[value] || value || '-'
}

const formatTraceAnalysisStatus = (value) => {
    const labelMap = {
        ANALYZED: '已分析',
        FAILED: '分析失败',
        TOOL_MISSING: '缺少工具',
        TRACE_MISSING: '文件缺失',
    }
    return labelMap[value] || value || '-'
}

const getPrimaryTraceCause = (artifact) => {
    const causes = artifact?.analysis?.suspected_causes
    if (Array.isArray(causes) && causes.length > 0) {
        return causes[0]?.title || '-'
    }
    return '-'
}

const getTopBusyThread = (artifact) => {
    const threads = artifact?.analysis?.top_busy_threads
    if (Array.isArray(threads) && threads.length > 0) {
        const thread = threads[0]
        return `${thread.thread_name || '-'} (${thread.running_ms || 0} ms)`
    }
    return '-'
}

const getTraceAnalysisLevel = (artifact) => {
    const level = artifact?.analysis?.analysis_level
    if (level === 'full') return '完整'
    if (level === 'frame_timeline_only') return '帧级分析'
    if (level === 'partial') return '部分'
    return '-'
}

const getTraceFrameStats = (artifact) => artifact?.analysis?.frame_stats || {}
const getTraceCaptureMode = (artifact) => artifact?.capture_mode || 'diagnostic'
const getTraceCaptureModeLabel = (artifact) => {
    const mode = getTraceCaptureMode(artifact)
    if (mode === 'continuous') return '全程采样'
    if (mode === 'diagnostic') return '异常诊断'
    return mode || '-'
}

const getTraceFrameTimelineConclusion = (artifact) => {
    const stats = getTraceFrameStats(artifact)
    const cadenceFps = Number(stats.cadence_fps)
    const effectiveFps = Number(stats.effective_fps)
    const p95Delay = Number(stats.present_delay_p95_ms)

    if (
        (!Number.isFinite(cadenceFps) || cadenceFps <= 0) &&
        (!Number.isFinite(effectiveFps) || effectiveFps <= 0) &&
        (!Number.isFinite(p95Delay) || p95Delay <= 0)
    ) {
        return '-'
    }

    const fpsPart = Number.isFinite(cadenceFps) && cadenceFps > 0
        ? `${formatMetric(effectiveFps)} / ${formatMetric(cadenceFps)} Hz`
        : `${formatMetric(effectiveFps)} Hz`
    const delayPart = Number.isFinite(p95Delay)
        ? `P95 延迟 ${formatMetric(Math.max(0, p95Delay), 1)} ms`
        : 'P95 延迟 -'
    return `${fpsPart} · ${delayPart}`
}

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

const getTraceTimelineSeries = (artifact) => (
    Array.isArray(artifact?.analysis?.frame_timeline_series) ? artifact.analysis.frame_timeline_series : []
)

const continuousTraceCount = computed(() => (
    traceArtifacts.value.filter(artifact => getTraceCaptureMode(artifact) === 'continuous').length
))

const diagnosticTraceCount = computed(() => (
    traceArtifacts.value.filter(artifact => getTraceCaptureMode(artifact) === 'diagnostic').length
))

const preferredTraceForCurve = computed(() => {
    const analyzedArtifacts = traceArtifacts.value
        .filter(artifact => artifact?.analysis_status === 'ANALYZED' && getTraceTimelineSeries(artifact).length > 0)
    if (analyzedArtifacts.length === 0) return null
    return analyzedArtifacts.find(artifact => getTraceCaptureMode(artifact) === 'continuous')
        || analyzedArtifacts[analyzedArtifacts.length - 1]
})

const resolveTraceBaseTime = (artifact) => {
    const captureStartedAt = artifact?.capture_started_at
    if (captureStartedAt) {
        const parsed = dayjs(captureStartedAt)
        if (parsed.isValid()) return parsed
    }

    const taskStart = dayjs(task.value?.started_at)
    if (!taskStart.isValid()) return null

    if (getTraceCaptureMode(artifact) === 'continuous') {
        return taskStart
    }

    const triggerTime = String(artifact?.trigger_time || '')
    const [hour, minute, second] = triggerTime.split(':').map(Number)
    if (![hour, minute, second].every(Number.isFinite)) return taskStart

    let triggerAt = taskStart.hour(hour).minute(minute).second(second).millisecond(0)
    if (triggerAt.isBefore(taskStart.subtract(12, 'hour'))) {
        triggerAt = triggerAt.add(1, 'day')
    }
    return triggerAt
}

const traceCurveSeries = computed(() => {
    const artifact = preferredTraceForCurve.value
    if (!artifact) return []
    const baseTime = resolveTraceBaseTime(artifact)
    if (!baseTime) return []
    return getTraceTimelineSeries(artifact).map(point => ({
        timestamp: baseTime
            .add(((Number(point.offset_sec) || 0) + (Number(point.window_sec) || 0)) * 1000, 'millisecond')
            .valueOf(),
        effectiveFps: Number(point.effective_fps || 0),
        cadenceFps: Number(point.cadence_fps || 0),
        jankRate: Number(((Number(point.jank_rate) || 0) * 100).toFixed(1)),
    }))
})

const jankChartOption = computed(() => {
    const gfxSeries = buildClockSeries(
        jankData.value.map(point => ({
            ...point,
            jankRatePercent: Number(((point.jank_rate || 0) * 100).toFixed(1)),
        })),
        'jankRatePercent',
    )
    const tracePoints = traceCurveSeries.value
    const traceFpsSeries = tracePoints
        .map(point => [point.timestamp, point.effectiveFps])
        .filter(point => Number.isFinite(point[0]) && Number.isFinite(point[1]))
    const maxJankRateValue = gfxSeries.reduce((best, current) => (
        Number.isFinite(current?.[1]) ? Math.max(best, current[1]) : best
    ), 0)
    const jankAxisMax = Math.ceil(Math.max(30, maxJankRateValue * 1.5))
    const hasTraceCurve = tracePoints.length > 0
    const traceCurveLabel = preferredTraceForCurve.value && getTraceCaptureMode(preferredTraceForCurve.value) === 'continuous'
        ? '实际流畅帧率'
        : '实际流畅帧率（局部）'

    return {
        title: {
            text: hasTraceCurve ? '卡顿帧监控（Trace FPS + gfxinfo 卡顿率）' : '卡顿帧监控（gfxinfo 触发信号）',
            left: 'center',
            textStyle: { fontSize: 15, color: '#303133' },
        },
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'cross' },
        },
        legend: { data: hasTraceCurve ? [traceCurveLabel, '卡顿率 (%)'] : ['卡顿率 (%)'], top: 35 },
        toolbox: {
            right: 20,
            feature: { saveAsImage: {} },
        },
        grid: { left: 60, right: 60, top: 80, bottom: 60 },
        dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 10 }],
        xAxis: {
            type: 'time',
            boundaryGap: false,
            axisLabel: {
                formatter: (value) => formatAxisTime(value),
            },
        },
        yAxis: hasTraceCurve ? [
            {
                type: 'value',
                name: 'FPS',
                position: 'left',
                min: 0,
            },
            {
                type: 'value',
                name: '卡顿率 (%)',
                position: 'right',
                min: 0,
                max: jankAxisMax,
                axisLabel: { formatter: '{value}%' },
            },
        ] : {
            type: 'value',
            name: '卡顿率 (%)',
            min: 0,
            max: jankAxisMax,
            axisLabel: { formatter: '{value}%' },
        },
        series: [
            ...(hasTraceCurve ? [{
                name: traceCurveLabel,
                type: 'line',
                smooth: true,
                connectNulls: true,
                data: traceFpsSeries,
                yAxisIndex: 0,
                lineStyle: { color: '#409EFF', width: 2 },
                itemStyle: { color: '#409EFF' },
                areaStyle: { color: 'rgba(64,158,255,0.08)' },
            }] : []),
            {
                name: '卡顿率 (%)',
                type: 'line',
                smooth: true,
                data: gfxSeries,
                ...(hasTraceCurve ? { yAxisIndex: 1 } : {}),
                lineStyle: { color: '#F56C6C', width: 2 },
                itemStyle: { color: '#F56C6C' },
                areaStyle: { color: 'rgba(245,108,108,0.08)' },
                markLine: activeJankEventTime.value ? {
                    symbol: 'none',
                    label: {
                        show: true,
                        formatter: '当前事件',
                        color: '#F56C6C',
                    },
                    lineStyle: {
                        color: '#F56C6C',
                        type: 'dashed',
                        width: 1.5,
                    },
                    data: [{ xAxis: activeJankEventTimestamp.value }],
                } : undefined,
            },
        ],
    }
})

const handleChartClick = (params) => {
    if (params.componentType === 'markPoint' && params.data?._eventData) {
        openLogDialog(params.data._eventData)
    }
}

const getReplayMeta = (event) => (
    event && typeof event.replay === 'object' && event.replay
        ? event.replay
        : null
)

const getReplayFilename = (event) => {
    const replay = getReplayMeta(event)
    if (!replay) return ''
    if (replay.filename) return replay.filename
    const replayPath = String(replay.path || '')
    const parts = replayPath.split('/')
    return parts[parts.length - 1] || ''
}

const isReplayReady = (event) => {
    const replay = getReplayMeta(event)
    return replay?.status === 'READY' && Boolean(getReplayFilename(event))
}

const formatReplayStatus = (event) => {
    const replay = getReplayMeta(event)
    if (!replay) {
        return localReplayEnabled.value ? '未生成回放' : '未开启录制'
    }
    if (replay.status === 'READY') {
        const durationText = Number(replay.duration_sec) > 0 ? ` · ${replay.duration_sec}s` : ''
        return `已生成${durationText}`
    }
    if (replay.status === 'UNAVAILABLE') {
        return replay.error || '未采集到可用视频'
    }
    if (replay.status === 'SKIPPED') {
        return replay.error || '回放导出已跳过'
    }
    if (replay.status === 'FAILED') {
        return replay.error || '回放生成失败'
    }
    return replay.error || replay.status || '无回放'
}

const openReplayDialog = (event) => {
    if (!isReplayReady(event)) {
        ElMessage.warning(formatReplayStatus(event))
        return
    }
    currentReplayEvent.value = event
    replayDialogVisible.value = true
}

const openLogDialog = (event) => {
    logEventType.value = event.type
    logContent.value = event.full_log || '无日志数据'
    logDialogVisible.value = true
    // 重置 AI 分析状态
    aiResult.value = ''
    aiRenderedHtml.value = ''
    showAiResult.value = false
    aiTokenUsage.value = 0
    aiCached.value = false
}

// AI 智能分析
const analyzeLog = async (options = {}) => {
    if (!logContent.value || logContent.value === '无日志数据') {
        ElMessage.warning('没有可分析的日志内容')
        return
    }

    const forceRefresh = Boolean(options.forceRefresh)
    aiAnalyzing.value = true
    try {
        const res = await api.analyzeLog({
            log_text: logContent.value,
            package_name: task.value?.package_name || '',
            device_info: task.value?.device_serial || '',
            force_refresh: forceRefresh,
        })
        const data = res.data
        if (data.success) {
            aiResult.value = data.analysis_result
            aiRenderedHtml.value = md.render(data.analysis_result)
            aiTokenUsage.value = data.token_usage || 0
            aiCached.value = data.cached || false
            showAiResult.value = true
        } else {
            ElMessage.error('分析失败，请重试')
        }
    } catch (err) {
        const msg = err.response?.data?.detail || err.message || '分析请求失败'
        ElMessage.error(msg)
    } finally {
        aiAnalyzing.value = false
    }
}

// 重新分析
const reAnalyze = () => {
    showAiResult.value = false
    aiResult.value = ''
    aiRenderedHtml.value = ''
    aiCached.value = false
    analyzeLog({ forceRefresh: true })
}

const applyTraceAiResult = (artifact, data) => {
    if (artifact) {
        artifact.ai_summary = data.analysis_result
        artifact.ai_summary_cached = data.cached || false
    }
    traceAiResult.value = data.analysis_result
    traceAiRenderedHtml.value = md.render(data.analysis_result)
    traceAiTokenUsage.value = data.token_usage || 0
    traceAiCached.value = data.cached || false
}

const requestTraceAiSummary = async (artifact, options = {}) => {
    if (!artifact) return
    if (artifact.analysis_status !== 'ANALYZED') {
        ElMessage.warning('当前 Trace 还没有可用的结构化分析结果')
        return null
    }
    const forceRefresh = Boolean(options.forceRefresh)

    if (!forceRefresh && artifact.ai_summary) {
        return {
            analysis_result: artifact.ai_summary,
            token_usage: 0,
            cached: artifact.ai_summary_cached !== false,
        }
    }

    const res = await api.analyzeFastbotTrace(taskId, {
        trace_path: artifact.path,
        force_refresh: forceRefresh,
    })
    const data = res.data
    if (!data.success) {
        throw new Error('AI 总结生成失败')
    }
    return data
}

const openTraceAiDialog = async (artifact, options = {}) => {
    if (!artifact) return
    currentTraceArtifact.value = artifact
    traceAiDialogVisible.value = true
    traceAiResult.value = ''
    traceAiRenderedHtml.value = ''
    traceAiTokenUsage.value = 0
    traceAiCached.value = false

    traceAiAnalyzing.value = true
    try {
        const data = await requestTraceAiSummary(artifact, options)
        if (data) {
            applyTraceAiResult(artifact, data)
        }
    } catch (err) {
        const msg = err.response?.data?.detail || err.message || 'AI 总结生成失败'
        ElMessage.error(msg)
    } finally {
        traceAiAnalyzing.value = false
    }
}

const reAnalyzeTrace = async () => {
    if (!currentTraceArtifact.value) return
    currentTraceArtifact.value.ai_summary = ''
    currentTraceArtifact.value.ai_summary_cached = false
    await openTraceAiDialog(currentTraceArtifact.value, { forceRefresh: true })
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
                const data = await requestTraceAiSummary(artifact)
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
    jankEventTableRef.value?.setCurrentRow?.(row)
}

const findClosestJankEvent = (targetTimestamp, maxDeltaMs = 5000) => {
    if (!Number.isFinite(targetTimestamp)) return null
    let bestRow = null
    let minDelta = Infinity
    jankEvents.value.forEach((row) => {
        const rowTimestamp = clockTimeToTimestamp(row.time)
        if (!Number.isFinite(rowTimestamp)) return
        const delta = Math.abs(rowTimestamp - targetTimestamp)
        if (delta < minDelta) {
            minDelta = delta
            bestRow = row
        }
    })
    return minDelta <= maxDeltaMs ? bestRow : null
}

const handleJankChartClick = (params) => {
    const rawValue = Array.isArray(params?.value)
        ? params.value[0]
        : Array.isArray(params?.data?.value)
            ? params.data.value[0]
            : (params?.axisValue ?? params?.name)
    const targetTimestamp = typeof rawValue === 'number'
        ? rawValue
        : clockTimeToTimestamp(rawValue)
    const matched = findClosestJankEvent(targetTimestamp)
    if (matched) {
        focusJankEvent(matched)
    }
}

const handleJankEventRowClick = (row) => {
    focusJankEvent(row)
}

const jankEventRowClassName = ({ row }) => (
    row?.time && row.time === activeJankEventTime.value ? 'active-jank-row' : ''
)

const formatTime = (t) => {
    if (!t) return '-'
    return dayjs(t).format('YYYY-MM-DD HH:mm:ss')
}

const formatDurationSeconds = (value) => {
    const total = Number(value || 0)
    if (!Number.isFinite(total) || total <= 0) return '-'
    if (total < 60) return `${total}s`
    const minute = Math.floor(total / 60)
    const second = total % 60
    return `${minute}m ${second}s`
}

const goBack = () => {
    router.push({ path: '/execution/reports', query: { tab: 'fastbot' } })
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
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">状态</div>
                    <div class="stat-value">
                        <el-tag :type="task.status === 'COMPLETED' ? 'success' : 'danger'" effect="plain">{{ task.status }}</el-tag>
                    </div>
                </el-card>
                <el-card v-if="performanceMonitorEnabled" shadow="never" class="stat-card">
                    <div class="stat-label">平均 CPU</div>
                    <div class="stat-value primary">{{ summary.avg_cpu || 0 }}%</div>
                </el-card>
                <el-card v-if="performanceMonitorEnabled" shadow="never" class="stat-card">
                    <div class="stat-label">峰值 CPU</div>
                    <div class="stat-value">{{ summary.max_cpu || 0 }}%</div>
                </el-card>
                <el-card v-if="performanceMonitorEnabled" shadow="never" class="stat-card">
                    <div class="stat-label">平均内存</div>
                    <div class="stat-value success">{{ summary.avg_mem || 0 }} MB</div>
                </el-card>
                <el-card v-if="performanceMonitorEnabled" shadow="never" class="stat-card">
                    <div class="stat-label">峰值内存</div>
                    <div class="stat-value">{{ summary.max_mem || 0 }} MB</div>
                </el-card>
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">崩溃次数</div>
                    <div class="stat-value danger">{{ summary.total_crashes || 0 }}</div>
                </el-card>
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">ANR 次数</div>
                    <div class="stat-value warning">{{ summary.total_anrs || 0 }}</div>
                </el-card>
            </div>

            <div v-if="jankFrameMonitorEnabled" class="summary-cards">
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">监控模式</div>
                    <div class="stat-value primary">{{ jankMonitoringModeLabel }}</div>
                </el-card>
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">全程 Trace</div>
                    <div class="stat-value primary">{{ continuousTraceCount }}</div>
                </el-card>
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">异常 Trace</div>
                    <div class="stat-value warning">{{ diagnosticTraceCount }}</div>
                </el-card>
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">活跃窗口平均卡顿率</div>
                    <div class="stat-value warning">{{ formatPercent(summary.active_avg_jank_rate) }}</div>
                </el-card>
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">最大卡顿率</div>
                    <div class="stat-value danger">{{ formatPercent(summary.max_jank_rate) }}</div>
                </el-card>
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">最差窗口时间点</div>
                    <div class="stat-value danger">{{ summary.peak_jank_rate_window?.time || '--' }}</div>
                </el-card>
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">严重卡顿次数</div>
                    <div class="stat-value danger">{{ summary.severe_jank_events || 0 }}</div>
                </el-card>
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">已分析 Trace</div>
                    <div class="stat-value success">{{ summary.analyzed_trace_count || 0 }}</div>
                </el-card>
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">目标帧率</div>
                    <div class="stat-value primary">
                        {{ traceFrameTimelineSummary ? formatMetric(traceFrameTimelineSummary.cadenceFps) : '--' }}
                    </div>
                </el-card>
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">实际流畅帧率</div>
                    <div class="stat-value success">
                        {{ traceFrameTimelineSummary ? formatMetric(traceFrameTimelineSummary.effectiveFps) : '--' }}
                    </div>
                </el-card>
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">Trace P95 呈现延迟</div>
                    <div class="stat-value warning">
                        {{ traceFrameTimelineSummary ? `${formatMetric(traceFrameTimelineSummary.p95DelayMs)} ms` : '--' }}
                    </div>
                </el-card>
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
            <el-card v-if="performanceMonitorEnabled" shadow="never" class="chart-card">
                <VChart
                    v-if="perfData.length > 0"
                    :option="chartOption"
                    :group="chartGroup"
                    autoresize
                    style="height: 400px; width: 100%"
                    @click="handleChartClick"
                />
                <el-empty v-else description="暂无性能数据" />
            </el-card>

            <el-card v-if="jankFrameMonitorEnabled" shadow="never" class="chart-card">
                <VChart
                    v-if="jankData.length > 0"
                    :option="jankChartOption"
                    :group="chartGroup"
                    autoresize
                    style="height: 400px; width: 100%"
                    @click="handleJankChartClick"
                />
                <el-empty v-else description="暂无卡顿监控数据" />
            </el-card>

            <!-- 异常事件列表 -->
            <el-card shadow="never" class="events-card" v-if="crashEvents.length > 0">
                <template #header>
                    <span class="card-title">异常事件记录 ({{ crashEvents.length }})</span>
                </template>
                <el-table :data="crashEvents" :header-cell-style="{ background: '#f5f7fa', color: '#606266' }">
                    <el-table-column label="时间" prop="time" width="120" align="center" />
                    <el-table-column label="类型" width="100" align="center">
                        <template #default="{ row }">
                            <el-tag :type="row.type === 'ANR' ? 'warning' : 'danger'" size="small">{{ row.type }}</el-tag>
                        </template>
                    </el-table-column>
                    <el-table-column label="日志" width="120" align="center">
                        <template #default="{ row }">
                            <el-button
                                v-if="row.full_log"
                                link
                                type="primary"
                                @click="openLogDialog(row)"
                            >
                                查看日志
                            </el-button>
                            <span v-else class="text-gray">无日志</span>
                        </template>
                    </el-table-column>
                    <el-table-column label="本地回放" min-width="220">
                        <template #default="{ row }">
                            <el-button
                                v-if="isReplayReady(row)"
                                link
                                type="primary"
                                @click="openReplayDialog(row)"
                            >
                                查看回放
                            </el-button>
                            <span v-else class="text-gray">{{ formatReplayStatus(row) }}</span>
                        </template>
                    </el-table-column>
                </el-table>
            </el-card>

            <el-card v-if="jankFrameMonitorEnabled" shadow="never" class="events-card">
                <template #header>
                    <span class="card-title">卡顿事件记录 ({{ jankEvents.length }})</span>
                </template>
                <el-table
                    v-if="jankEvents.length > 0"
                    ref="jankEventTableRef"
                    :data="jankEvents"
                    :header-cell-style="{ background: '#f5f7fa', color: '#606266' }"
                    :row-class-name="jankEventRowClassName"
                    highlight-current-row
                    @row-click="handleJankEventRowClick"
                >
                    <el-table-column label="时间" prop="time" width="120" align="center" />
                    <el-table-column label="等级" width="100" align="center">
                        <template #default="{ row }">
                            <el-tag :type="row.severity === 'CRITICAL' ? 'danger' : 'warning'" size="small">
                                {{ formatJankSeverity(row.severity) }}
                            </el-tag>
                        </template>
                    </el-table-column>
                    <el-table-column label="原因" min-width="140">
                        <template #default="{ row }">{{ formatJankReason(row.reason) }}</template>
                    </el-table-column>
                    <el-table-column label="卡顿率" width="120" align="center">
                        <template #default="{ row }">{{ formatPercent(row.jank_rate) }}</template>
                    </el-table-column>
                    <el-table-column label="CPU" width="100" align="center">
                        <template #default="{ row }">{{ row.cpu === null || row.cpu === undefined ? '-' : `${row.cpu}%` }}</template>
                    </el-table-column>
                    <el-table-column label="内存" width="110" align="center">
                        <template #default="{ row }">{{ row.mem === null || row.mem === undefined ? '-' : `${row.mem} MB` }}</template>
                    </el-table-column>
                    <el-table-column label="总帧数" width="100" align="center" prop="total_frames" />
                    <el-table-column label="卡顿帧" width="100" align="center" prop="jank_frames" />
                    <el-table-column label="Trace" width="120" align="center">
                        <template #default="{ row }">
                            <el-tag :type="row.trace_exported ? 'success' : 'info'" size="small">
                                {{ row.trace_exported ? '已导出' : '未导出' }}
                            </el-tag>
                        </template>
                    </el-table-column>
                    <el-table-column label="诊断状态" width="120" align="center">
                        <template #default="{ row }">{{ formatDiagnosisStatus(row.diagnosis_status) }}</template>
                    </el-table-column>
                    <el-table-column label="诊断摘要" min-width="220" prop="diagnosis_summary" show-overflow-tooltip />
                    <el-table-column label="数据源" width="120" align="center">
                        <template #default="{ row }">{{ formatJankSource(row.source) }}</template>
                    </el-table-column>
                </el-table>
                <el-empty v-else description="暂无卡顿事件" />
            </el-card>

            <el-card v-if="jankFrameMonitorEnabled" shadow="never" class="events-card">
                <template #header>
                    <div class="trace-header">
                        <span class="card-title">Perfetto Trace ({{ traceArtifacts.length }})</span>
                        <el-button
                            type="primary"
                            link
                            :loading="batchTraceAiLoading"
                            :disabled="analyzedTraceArtifacts.length === 0"
                            @click="generateAllTraceSummaries"
                        >
                            全部生成 AI 总结
                        </el-button>
                    </div>
                </template>
                <el-table
                    v-if="traceArtifacts.length > 0"
                    :data="traceArtifacts"
                    :header-cell-style="{ background: '#f5f7fa', color: '#606266' }"
                >
                    <el-table-column type="expand">
                        <template #default="{ row }">
                            <el-descriptions :column="2" border size="small" class="trace-detail-block">
                                <el-descriptions-item label="触发原因">{{ formatJankReason(row.trigger_reason) }}</el-descriptions-item>
                                <el-descriptions-item label="分析状态">{{ formatTraceAnalysisStatus(row.analysis_status) }}</el-descriptions-item>
                                <el-descriptions-item label="分析层级">{{ getTraceAnalysisLevel(row) }}</el-descriptions-item>
                                <el-descriptions-item label="FrameTimeline">{{ row.frame_timeline_supported ? '支持' : '不支持' }}</el-descriptions-item>
                                <el-descriptions-item label="最忙线程">{{ getTopBusyThread(row) }}</el-descriptions-item>
                                <el-descriptions-item label="FrameTimeline 结论">{{ getTraceFrameTimelineConclusion(row) }}</el-descriptions-item>
                                <el-descriptions-item label="Trace 路径" :span="2">{{ row.path || '-' }}</el-descriptions-item>
                            </el-descriptions>
                        </template>
                    </el-table-column>
                    <el-table-column label="触发时间" prop="trigger_time" width="140" align="center" />
                    <el-table-column label="采集模式" width="120" align="center">
                        <template #default="{ row }">{{ getTraceCaptureModeLabel(row) }}</template>
                    </el-table-column>
                    <el-table-column label="一句话结论" min-width="280" show-overflow-tooltip>
                        <template #default="{ row }">{{ getPrimaryTraceCause(row) }}</template>
                    </el-table-column>
                    <el-table-column label="AI 总结" width="120" align="center">
                        <template #default="{ row }">
                            <el-button
                                link
                                type="primary"
                                :disabled="row.analysis_status !== 'ANALYZED'"
                                @click="openTraceAiDialog(row)"
                            >
                                {{ row.ai_summary ? '查看' : '生成' }}
                            </el-button>
                        </template>
                    </el-table-column>
                </el-table>
                <el-empty v-else description="暂无导出的 Perfetto Trace" />
            </el-card>

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
        </div>

        <!-- 日志查看弹窗 (含 AI 分析) -->
        <el-dialog
            v-model="logDialogVisible"
            :title="`${logEventType} 日志快照`"
            width="80%"
            top="5vh"
            destroy-on-close
        >
            <!-- 原始日志 -->
            <pre class="log-viewer">{{ logContent }}</pre>

            <!-- AI 分析区域 -->
            <el-divider content-position="center">
                <span style="color: #909399; font-size: 12px;">AI 智能分析</span>
            </el-divider>

            <!-- 分析按钮 (未分析时显示) -->
            <div class="ai-action-area" v-if="!showAiResult">
                <el-button
                    type="primary"
                    :icon="MagicStick"
                    :loading="aiAnalyzing"
                    :loading-text="'正在分析中...'"
                    size="large"
                    round
                    @click="analyzeLog"
                >
                    ✨ AI 智能根因分析
                </el-button>
                <p class="ai-hint" v-if="!aiAnalyzing">点击按钮，AI 将自动提取关键日志并给出根因分析与修复建议</p>
                <p class="ai-hint analyzing" v-else>正在清洗日志并调用 AI 模型，请稍候...</p>
            </div>

            <!-- 分析结果卡片 -->
            <el-card v-if="showAiResult" class="ai-analysis-card" shadow="hover">
                <template #header>
                    <div class="ai-card-header">
                        <span class="ai-card-title">🤖 AI 诊断报告</span>
                        <div class="ai-card-actions">
                            <el-tag v-if="aiCached" type="info" size="small" effect="plain">缓存结果</el-tag>
                            <el-tag v-if="aiTokenUsage > 0" type="warning" size="small" effect="plain">Token: {{ aiTokenUsage }}</el-tag>
                            <el-button
                                :icon="RefreshRight"
                                size="small"
                                text
                                type="primary"
                                @click="reAnalyze"
                            >
                                重新分析
                            </el-button>
                        </div>
                    </div>
                </template>
                <div class="ai-markdown-body" v-html="aiRenderedHtml"></div>
            </el-card>
        </el-dialog>

        <el-dialog
            v-model="replayDialogVisible"
            :title="currentReplayTitle"
            width="70%"
            top="8vh"
            destroy-on-close
        >
            <FastbotReplayPlayer
                v-if="replayDialogVisible && currentReplayEvent"
                :key="`${currentReplayEvent.time}-${getReplayFilename(currentReplayEvent)}`"
                :task-id="taskId"
                :filename="getReplayFilename(currentReplayEvent)"
            />
        </el-dialog>

        <el-dialog
            v-model="traceAiDialogVisible"
            title="Perfetto Trace AI 总结"
            width="70%"
            top="8vh"
            destroy-on-close
        >
            <div class="ai-action-area" v-if="!traceAiResult && !traceAiAnalyzing">
                <el-button
                    type="primary"
                    :icon="MagicStick"
                    size="large"
                    round
                    @click="openTraceAiDialog(currentTraceArtifact)"
                >
                    ✨ 生成 AI 总结
                </el-button>
                <p class="ai-hint">AI 只会基于当前 Trace 片段生成结论，不代表整段录制的整体体验。</p>
            </div>
            <div class="ai-action-area" v-else-if="traceAiAnalyzing">
                <el-button
                    type="primary"
                    :icon="MagicStick"
                    :loading="true"
                    :loading-text="'正在分析中...'"
                    size="large"
                    round
                >
                    ✨ 正在生成 AI 总结
                </el-button>
                <p class="ai-hint analyzing">正在调用模型，请稍候...</p>
            </div>
            <el-card v-else class="ai-analysis-card" shadow="hover">
                <template #header>
                    <div class="ai-card-header">
                        <span class="ai-card-title">🤖 AI 卡顿诊断</span>
                        <div class="ai-card-actions">
                            <el-tag v-if="traceAiCached" type="info" size="small" effect="plain">缓存结果</el-tag>
                            <el-tag v-if="traceAiTokenUsage > 0" type="warning" size="small" effect="plain">Token: {{ traceAiTokenUsage }}</el-tag>
                            <el-button
                                :icon="RefreshRight"
                                size="small"
                                text
                                type="primary"
                                @click="reAnalyzeTrace"
                            >
                                重新分析
                            </el-button>
                        </div>
                    </div>
                </template>
                <div class="ai-markdown-body" v-html="traceAiRenderedHtml"></div>
            </el-card>
        </el-dialog>
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

.stat-card {
    flex: 1;
    min-width: 120px;
    text-align: center;
}

.stat-card :deep(.el-card__body) {
    padding: 16px 12px;
}

.stat-label {
    font-size: 12px;
    color: #909399;
    margin-bottom: 6px;
}

.stat-value {
    font-size: 20px;
    font-weight: 700;
    color: #303133;
}

.stat-value.primary { color: #409EFF; }
.stat-value.success { color: #67C23A; }
.stat-value.danger { color: #F56C6C; }
.stat-value.warning { color: #E6A23C; }

.chart-card, .events-card, .info-card {
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

.trace-detail-block {
    margin: 8px 0;
}

.card-title {
    font-size: 14px;
    font-weight: 600;
    color: #303133;
}

.text-gray { color: #909399; font-size: 13px; }
.trace-insight-hint {
    font-size: 12px;
    color: #606266;
    padding: 0 4px;
}

.events-card :deep(.active-jank-row) {
    --el-table-tr-bg-color: #fff3f0;
}

/* ==================== 日志查看器 ==================== */
.log-viewer {
    background: #1e1e1e;
    color: #d4d4d4;
    font-family: 'Menlo', 'Monaco', 'Courier New', monospace;
    font-size: 12px;
    line-height: 1.5;
    padding: 16px;
    border-radius: 6px;
    max-height: 50vh;
    overflow: auto;
    white-space: pre-wrap;
    word-break: break-all;
    margin: 0;
}

/* ==================== AI 分析区域 ==================== */
.ai-action-area {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 24px 0 16px;
    gap: 12px;
}

.ai-hint {
    font-size: 12px;
    color: #909399;
    margin: 0;
}

.ai-hint.analyzing {
    color: #409EFF;
    animation: pulse 1.5s ease-in-out infinite;
}

@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
}

/* AI 分析结果卡片 */
.ai-analysis-card {
    margin-top: 8px;
    border: 1px solid #e4e7ed;
    border-radius: 8px;
    background: linear-gradient(135deg, #fafbff 0%, #f5f7ff 100%);
}

.ai-analysis-card :deep(.el-card__header) {
    padding: 12px 20px;
    background: linear-gradient(90deg, #ecf0ff 0%, #f5f0ff 100%);
    border-bottom: 1px solid #e4e7ed;
}

.ai-card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.ai-card-title {
    font-size: 15px;
    font-weight: 600;
    color: #303133;
}

.ai-card-actions {
    display: flex;
    align-items: center;
    gap: 8px;
}

/* ==================== Markdown 渲染样式 ==================== */
.ai-markdown-body {
    font-size: 14px;
    line-height: 1.8;
    color: #303133;
    padding: 4px 0;
}

.ai-markdown-body :deep(h3) {
    font-size: 16px;
    font-weight: 700;
    color: #409EFF;
    margin: 16px 0 8px 0;
    padding-bottom: 6px;
    border-bottom: 2px solid #e6ecf5;
}

.ai-markdown-body :deep(h4) {
    font-size: 14px;
    font-weight: 600;
    color: #606266;
    margin: 12px 0 6px 0;
}

.ai-markdown-body :deep(p) {
    margin: 6px 0;
    color: #606266;
}

.ai-markdown-body :deep(strong) {
    color: #303133;
    font-weight: 600;
}

.ai-markdown-body :deep(code) {
    background: #f0f2f5;
    color: #c7254e;
    padding: 2px 6px;
    border-radius: 3px;
    font-family: 'Menlo', 'Monaco', 'Courier New', monospace;
    font-size: 13px;
}

.ai-markdown-body :deep(pre) {
    background: #1e1e1e;
    color: #d4d4d4;
    padding: 12px 16px;
    border-radius: 6px;
    overflow-x: auto;
    margin: 8px 0;
    font-size: 13px;
    line-height: 1.5;
}

.ai-markdown-body :deep(pre code) {
    background: transparent;
    color: inherit;
    padding: 0;
    border-radius: 0;
}

.ai-markdown-body :deep(ol),
.ai-markdown-body :deep(ul) {
    padding-left: 24px;
    margin: 6px 0;
}

.ai-markdown-body :deep(li) {
    margin: 4px 0;
    color: #606266;
}

.ai-markdown-body :deep(blockquote) {
    border-left: 4px solid #409EFF;
    padding: 8px 16px;
    margin: 8px 0;
    background: #f5f7fa;
    color: #606266;
    border-radius: 0 4px 4px 0;
}

.ai-markdown-body :deep(hr) {
    border: none;
    border-top: 1px solid #ebeef5;
    margin: 12px 0;
}
</style>
