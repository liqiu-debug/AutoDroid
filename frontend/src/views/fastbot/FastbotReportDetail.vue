<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, MagicStick, RefreshRight } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import dayjs from 'dayjs'
import VChart from 'vue-echarts'
import MarkdownIt from 'markdown-it'
import { use } from 'echarts/core'
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
const task = ref(null)
const report = ref(null)
const loading = ref(true)

// 日志弹窗
const logDialogVisible = ref(false)
const logContent = ref('')
const logEventType = ref('')

// AI 分析状态
const aiAnalyzing = ref(false)
const aiResult = ref('')
const aiRenderedHtml = ref('')
const aiTokenUsage = ref(0)
const aiCached = ref(false)
const showAiResult = ref(false)

const devicesMap = ref({})

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
const crashEvents = computed(() => report.value?.crash_events || [])
const summary = computed(() => report.value?.summary || {})

const chartOption = computed(() => {
    const times = perfData.value.map(p => p.time)
    const cpuValues = perfData.value.map(p => p.cpu)
    const memValues = perfData.value.map(p => p.mem)

    const timeToSeconds = (t) => {
        const parts = t.split(':').map(Number)
        return (parts[0] || 0) * 3600 + (parts[1] || 0) * 60 + (parts[2] || 0)
    }

    const findClosestIndex = (targetTime) => {
        if (times.length === 0) return -1
        const targetSec = timeToSeconds(targetTime)
        let closest = 0
        let minDiff = Infinity
        times.forEach((t, i) => {
            const diff = Math.abs(timeToSeconds(t) - targetSec)
            if (diff < minDiff) { closest = i; minDiff = diff }
        })
        return closest
    }

    const crashMarkPoints = crashEvents.value
        .map(e => {
            let idx = times.indexOf(e.time)
            if (idx === -1) {
                idx = findClosestIndex(e.time)
            }
            if (idx === -1) return null
            return {
                coord: [idx, cpuValues[idx]],
                itemStyle: { color: e.type === 'ANR' ? '#E6A23C' : '#F56C6C' },
                symbol: 'pin',
                symbolSize: 40,
                value: e.type,
                _eventData: e,
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
        xAxis: { type: 'category', data: times, boundaryGap: false },
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
                data: cpuValues,
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
                data: memValues,
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
        openLogDialog(params.data._eventData)
    }
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
const analyzeLog = async () => {
    if (!logContent.value || logContent.value === '无日志数据') {
        ElMessage.warning('没有可分析的日志内容')
        return
    }

    aiAnalyzing.value = true
    try {
        const res = await api.analyzeLog({
            log_text: logContent.value,
            package_name: task.value?.package_name || '',
            device_info: task.value?.device_serial || '',
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
    analyzeLog()
}

const formatTime = (t) => {
    if (!t) return '-'
    return dayjs(t).format('YYYY-MM-DD HH:mm:ss')
}

const goBack = () => {
    router.push({ path: '/execution/reports', query: { tab: 'fastbot' } })
}

onMounted(() => {
    fetchData()
})
</script>

<template>
    <div class="report-detail-container" v-loading="loading">
        <!-- 顶部导航栏 -->
        <div class="top-bar">
            <el-button text :icon="ArrowLeft" @click="goBack">返回列表</el-button>
            <span class="title" v-if="task">
                性能报告 — {{ task.package_name }}
            </span>
        </div>

        <div class="detail-body" v-if="task && report">
            <!-- 任务概要卡片 -->
            <div class="summary-cards">
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">状态</div>
                    <div class="stat-value">
                        <el-tag :type="task.status === 'COMPLETED' ? 'success' : 'danger'" effect="plain">{{ task.status }}</el-tag>
                    </div>
                </el-card>
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">平均 CPU</div>
                    <div class="stat-value primary">{{ summary.avg_cpu || 0 }}%</div>
                </el-card>
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">峰值 CPU</div>
                    <div class="stat-value">{{ summary.max_cpu || 0 }}%</div>
                </el-card>
                <el-card shadow="never" class="stat-card">
                    <div class="stat-label">平均内存</div>
                    <div class="stat-value success">{{ summary.avg_mem || 0 }} MB</div>
                </el-card>
                <el-card shadow="never" class="stat-card">
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

            <!-- 性能折线图 -->
            <el-card shadow="never" class="chart-card">
                <VChart
                    v-if="perfData.length > 0"
                    :option="chartOption"
                    autoresize
                    style="height: 400px; width: 100%"
                    @click="handleChartClick"
                />
                <el-empty v-else description="暂无性能数据" />
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
                    <el-table-column label="操作" width="120" align="center">
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
                </el-table>
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

.card-title {
    font-size: 14px;
    font-weight: 600;
    color: #303133;
}

.text-gray { color: #909399; font-size: 13px; }

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
