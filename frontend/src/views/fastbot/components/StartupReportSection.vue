<script setup>
import { computed } from 'vue'
import StatCard from './StatCard.vue'
import {
    formatDiagnosisStatus,
    formatMs,
    formatReadyStatus,
    formatStartupMode,
    formatTime,
    formatTraceAnalysisStatus,
    getPrimaryTraceCause,
} from './reportFormatters'

const props = defineProps({
    task: { type: Object, default: null },
    summary: { type: Object, default: () => ({}) },
    traceArtifacts: { type: Array, default: () => [] },
    deviceName: { type: String, default: '' },
    analyzedCount: { type: Number, default: 0 },
    batchLoading: { type: Boolean, default: false },
})

defineEmits(['open-trace-ai', 'generate-summaries'])

const startupConfig = computed(() => props.summary?.startup_config || {})
const startupRuns = computed(() => Array.isArray(props.summary?.startup_runs) ? props.summary.startup_runs : [])
const startupAggregate = computed(() => props.summary?.startup_aggregate || {})
const slowEvents = computed(() => Array.isArray(props.summary?.slow_events) ? props.summary.slow_events : [])
const startupModesLabel = computed(() => {
    const modes = startupConfig.value?.startup_modes || []
    if (!Array.isArray(modes) || modes.length === 0) return '-'
    return modes.map(mode => mode === 'cold' ? '冷启动' : '热启动').join(' / ')
})
const startupSuccessRate = computed(() => `${((Number(props.summary?.success_rate) || 0) * 100).toFixed(1)}%`)
</script>

<template>
    <div class="summary-cards">
        <StatCard label="状态">
            <el-tag :type="task.status === 'COMPLETED' ? 'success' : (task.status === 'FAILED' ? 'danger' : 'warning')" effect="plain">
                {{ task.status }}
            </el-tag>
        </StatCard>
        <StatCard label="冷启动就绪 P90" tone="primary" :value="formatMs(startupAggregate.cold?.ready_p90_ms)" />
        <StatCard label="热启动就绪 P90" tone="success" :value="formatMs(startupAggregate.hot?.ready_p90_ms)" />
        <StatCard label="冷启动 Total P90" :value="formatMs(startupAggregate.cold?.p90_ms)" />
        <StatCard label="热启动 Total P90" :value="formatMs(startupAggregate.hot?.p90_ms)" />
        <StatCard label="成功率" tone="success" :value="startupSuccessRate" />
        <StatCard label="慢启动次数" tone="danger" :value="summary.slow_count || 0" />
        <StatCard label="Trace 数" tone="warning" :value="traceArtifacts.length" />
    </div>

    <el-card shadow="never" class="info-card">
        <template #header>
            <span class="card-title">启动测试配置</span>
        </template>
        <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="包名">{{ task.package_name }}</el-descriptions-item>
            <el-descriptions-item label="Activity">{{ startupConfig.resolved_component || startupConfig.activity_name || '-' }}</el-descriptions-item>
            <el-descriptions-item label="设备">{{ deviceName }}</el-descriptions-item>
            <el-descriptions-item label="启动模式">{{ startupModesLabel }}</el-descriptions-item>
            <el-descriptions-item label="启动次数">{{ startupConfig.iterations || task.duration }}</el-descriptions-item>
            <el-descriptions-item label="轮次间隔">{{ startupConfig.cooldown_sec ?? '-' }}s</el-descriptions-item>
            <el-descriptions-item label="冷启动阈值">{{ formatMs(startupConfig.perfetto_slow_trace?.cold_threshold_ms) }}</el-descriptions-item>
            <el-descriptions-item label="热启动阈值">{{ formatMs(startupConfig.perfetto_slow_trace?.hot_threshold_ms) }}</el-descriptions-item>
            <el-descriptions-item label="首页就绪检查">{{ startupConfig.ready_check?.enabled ? '已开启' : '未开启' }}</el-descriptions-item>
            <el-descriptions-item label="开始时间">{{ formatTime(task.started_at) }}</el-descriptions-item>
            <el-descriptions-item label="结束时间">{{ formatTime(task.finished_at) }}</el-descriptions-item>
            <el-descriptions-item label="执行人">{{ task.executor_name || '-' }}</el-descriptions-item>
        </el-descriptions>
    </el-card>

    <el-card shadow="never" class="events-card">
        <template #header>
            <span class="card-title">启动轮次明细 ({{ startupRuns.length }})</span>
        </template>
        <el-table
            v-if="startupRuns.length > 0"
            :data="startupRuns"
            :header-cell-style="{ background: '#f5f7fa', color: '#606266' }"
        >
            <el-table-column label="模式" width="90" align="center">
                <template #default="{ row }">{{ formatStartupMode(row.mode) }}</template>
            </el-table-column>
            <el-table-column label="轮次" prop="iteration" width="70" align="center" />
            <el-table-column label="状态" width="90" align="center">
                <template #default="{ row }">
                    <el-tag :type="row.success ? 'success' : 'danger'" size="small" effect="plain">{{ row.status }}</el-tag>
                </template>
            </el-table-column>
            <el-table-column label="Activity" min-width="190" show-overflow-tooltip>
                <template #default="{ row }">{{ row.activity || '-' }}</template>
            </el-table-column>
            <el-table-column label="首页就绪" width="120" align="center">
                <template #default="{ row }">
                    {{ row.ready_ms ? formatMs(row.ready_ms) : formatReadyStatus(row.ready_status) }}
                </template>
            </el-table-column>
            <el-table-column label="TotalTime" width="110" align="center">
                <template #default="{ row }">{{ formatMs(row.total_time_ms) }}</template>
            </el-table-column>
            <el-table-column label="ThisTime" width="110" align="center">
                <template #default="{ row }">{{ formatMs(row.this_time_ms) }}</template>
            </el-table-column>
            <el-table-column label="WaitTime" width="110" align="center">
                <template #default="{ row }">{{ formatMs(row.wait_time_ms) }}</template>
            </el-table-column>
            <el-table-column label="Displayed" width="120" align="center">
                <template #default="{ row }">{{ formatMs(row.displayed?.time_ms) }}</template>
            </el-table-column>
            <el-table-column label="Fully drawn" width="120" align="center">
                <template #default="{ row }">{{ formatMs(row.fully_drawn?.time_ms) }}</template>
            </el-table-column>
            <el-table-column label="错误原因" min-width="220" show-overflow-tooltip>
                <template #default="{ row }">{{ row.error || row.ready_error || '-' }}</template>
            </el-table-column>
        </el-table>
        <el-empty v-else description="暂无启动轮次数据" />
    </el-card>

    <el-card shadow="never" class="events-card">
        <template #header>
            <span class="card-title">慢启动事件 ({{ slowEvents.length }})</span>
        </template>
        <el-table
            v-if="slowEvents.length > 0"
            :data="slowEvents"
            :header-cell-style="{ background: '#f5f7fa', color: '#606266' }"
        >
            <el-table-column label="模式" width="90" align="center">
                <template #default="{ row }">{{ formatStartupMode(row.mode) }}</template>
            </el-table-column>
            <el-table-column label="轮次" prop="iteration" width="70" align="center" />
            <el-table-column label="耗时" width="110" align="center">
                <template #default="{ row }">{{ formatMs(row.total_time_ms) }}</template>
            </el-table-column>
            <el-table-column label="阈值" width="110" align="center">
                <template #default="{ row }">{{ formatMs(row.threshold_ms) }}</template>
            </el-table-column>
            <el-table-column label="Trace" min-width="260" show-overflow-tooltip>
                <template #default="{ row }">{{ row.trace_path || row.trace_error || '未导出' }}</template>
            </el-table-column>
            <el-table-column label="诊断状态" width="120" align="center">
                <template #default="{ row }">{{ formatDiagnosisStatus(row.diagnosis_status) }}</template>
            </el-table-column>
        </el-table>
        <el-empty v-else description="没有触发慢启动阈值" />
    </el-card>

    <el-card shadow="never" class="events-card">
        <template #header>
            <div class="trace-header">
                <span class="card-title">Perfetto Trace ({{ traceArtifacts.length }})</span>
                <el-button
                    type="primary"
                    link
                    :loading="batchLoading"
                    :disabled="analyzedCount === 0"
                    @click="$emit('generate-summaries')"
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
            <el-table-column label="触发时间" prop="trigger_time" width="120" align="center" />
            <el-table-column label="模式" width="90" align="center">
                <template #default="{ row }">{{ formatStartupMode(row.startup_mode) }}</template>
            </el-table-column>
            <el-table-column label="诊断状态" width="120" align="center">
                <template #default="{ row }">{{ formatTraceAnalysisStatus(row.analysis_status) }}</template>
            </el-table-column>
            <el-table-column label="一句话结论" min-width="260" show-overflow-tooltip>
                <template #default="{ row }">{{ getPrimaryTraceCause(row) }}</template>
            </el-table-column>
            <el-table-column label="Trace 路径" min-width="260" prop="path" show-overflow-tooltip />
            <el-table-column label="AI 总结" width="120" align="center">
                <template #default="{ row }">
                    <el-button
                        link
                        type="primary"
                        :disabled="row.analysis_status !== 'ANALYZED'"
                        @click="$emit('open-trace-ai', row)"
                    >
                        {{ row.ai_summary ? '查看' : '生成' }}
                    </el-button>
                </template>
            </el-table-column>
        </el-table>
        <el-empty v-else description="暂无慢启动 Perfetto Trace" />
    </el-card>
</template>

<style scoped>
.summary-cards {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
}

.info-card,
.events-card {
    border-radius: 4px;
}

.trace-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
}

.card-title {
    font-size: 14px;
    font-weight: 600;
    color: #303133;
}
</style>
