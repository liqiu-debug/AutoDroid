<script setup>
import { computed } from 'vue'
import VChart from 'vue-echarts'
import './echartsSetup'
import {
    buildClockSeries,
    clockTimeToTimestamp,
    findClosestSeriesPoint,
    formatAxisTime,
    resolveReportBaseDate,
} from './reportFormatters'

const props = defineProps({
    perfData: { type: Array, default: () => [] },
    crashEvents: { type: Array, default: () => [] },
    startedAt: { type: String, default: '' },
    chartGroup: { type: String, default: '' },
})

const emit = defineEmits(['mark-point-click'])

const reportBaseDate = computed(() => resolveReportBaseDate(props.startedAt))

const chartOption = computed(() => {
    const cpuSeries = buildClockSeries(reportBaseDate.value, props.perfData, 'cpu')
    const memSeries = buildClockSeries(reportBaseDate.value, props.perfData, 'mem')

    const crashMarkPoints = props.crashEvents
        .map((event) => {
            const eventTimestamp = clockTimeToTimestamp(reportBaseDate.value, event.time)
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

const handleChartClick = (params) => {
    if (params.componentType === 'markPoint' && params.data?._eventData) {
        emit('mark-point-click', params.data._eventData)
    }
}
</script>

<template>
    <el-card shadow="never" class="chart-card">
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
</template>

<style scoped>
.chart-card {
    border-radius: 4px;
}
</style>
