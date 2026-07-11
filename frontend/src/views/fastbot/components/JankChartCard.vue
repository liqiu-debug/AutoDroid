<script setup>
import { computed } from 'vue'
import dayjs from 'dayjs'
import VChart from 'vue-echarts'
import './echartsSetup'
import {
    buildClockSeries,
    clockTimeToTimestamp,
    formatAxisTime,
    getTraceCaptureMode,
    getTraceTimelineSeries,
    resolveReportBaseDate,
} from './reportFormatters'

const props = defineProps({
    jankData: { type: Array, default: () => [] },
    traceArtifacts: { type: Array, default: () => [] },
    activeJankEventTime: { type: String, default: '' },
    startedAt: { type: String, default: '' },
    chartGroup: { type: String, default: '' },
})

const emit = defineEmits(['point-click'])

const reportBaseDate = computed(() => resolveReportBaseDate(props.startedAt))

const activeJankEventTimestamp = computed(() => clockTimeToTimestamp(reportBaseDate.value, props.activeJankEventTime))

const preferredTraceForCurve = computed(() => {
    const analyzedArtifacts = props.traceArtifacts
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

    const taskStart = dayjs(props.startedAt)
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
        reportBaseDate.value,
        props.jankData.map(point => ({
            ...point,
            jankRatePercent: Number(((point.jank_rate || 0) * 100).toFixed(1)),
        })),
        'jankRatePercent',
    )

    const hasFramestats = props.jankData.some(p => p.source === 'framestats')
    const framestatsFpsSeries = hasFramestats
        ? buildClockSeries(
            reportBaseDate.value,
            props.jankData.filter(p => !p.is_idle && p.fps > 0),
            'fps',
        )
        : []

    const tracePoints = traceCurveSeries.value
    const traceFpsSeries = tracePoints
        .map(point => [point.timestamp, point.effectiveFps])
        .filter(point => Number.isFinite(point[0]) && Number.isFinite(point[1]))

    const fpsSeries = framestatsFpsSeries.length > 0 ? framestatsFpsSeries : traceFpsSeries
    const hasFpsCurve = fpsSeries.length > 0
    const fpsLabel = framestatsFpsSeries.length > 0 ? '实时 FPS' : (
        preferredTraceForCurve.value && getTraceCaptureMode(preferredTraceForCurve.value) === 'continuous'
            ? '实际流畅帧率'
            : '实际流畅帧率（局部）'
    )

    const maxJankRateValue = gfxSeries.reduce((best, current) => (
        Number.isFinite(current?.[1]) ? Math.max(best, current[1]) : best
    ), 0)
    const jankAxisMax = Math.ceil(Math.max(30, maxJankRateValue * 1.5))

    const chartTitle = hasFpsCurve
        ? (hasFramestats ? '卡顿帧监控（逐帧 FPS + 卡顿率）' : '卡顿帧监控（Trace FPS + 卡顿率）')
        : '卡顿帧监控（卡顿率）'

    return {
        title: {
            text: chartTitle,
            left: 'center',
            textStyle: { fontSize: 15, color: '#303133' },
        },
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'cross' },
        },
        legend: { data: hasFpsCurve ? [fpsLabel, '卡顿率 (%)'] : ['卡顿率 (%)'], top: 35 },
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
        yAxis: hasFpsCurve ? [
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
            ...(hasFpsCurve ? [{
                name: fpsLabel,
                type: 'line',
                smooth: true,
                connectNulls: true,
                data: fpsSeries,
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
                ...(hasFpsCurve ? { yAxisIndex: 1 } : {}),
                lineStyle: { color: '#F56C6C', width: 2 },
                itemStyle: { color: '#F56C6C' },
                areaStyle: { color: 'rgba(245,108,108,0.08)' },
                markLine: props.activeJankEventTime ? {
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

const handleJankChartClick = (params) => {
    const rawValue = Array.isArray(params?.value)
        ? params.value[0]
        : Array.isArray(params?.data?.value)
            ? params.data.value[0]
            : (params?.axisValue ?? params?.name)
    const targetTimestamp = typeof rawValue === 'number'
        ? rawValue
        : clockTimeToTimestamp(reportBaseDate.value, rawValue)
    emit('point-click', targetTimestamp)
}
</script>

<template>
    <el-card shadow="never" class="chart-card">
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
</template>

<style scoped>
.chart-card {
    border-radius: 4px;
}
</style>
