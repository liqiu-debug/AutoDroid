<script setup>
import {
    formatJankReason,
    formatTraceAnalysisStatus,
    getPrimaryTraceCause,
    getTopBusyThread,
    getTraceAnalysisLevel,
    getTraceCaptureModeLabel,
    getTraceFrameTimelineConclusion,
} from './reportFormatters'

defineProps({
    traceArtifacts: { type: Array, default: () => [] },
    analyzedCount: { type: Number, default: 0 },
    batchLoading: { type: Boolean, default: false },
})

defineEmits(['open-trace-ai', 'generate-summaries'])
</script>

<template>
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
                        @click="$emit('open-trace-ai', row)"
                    >
                        {{ row.ai_summary ? '查看' : '生成' }}
                    </el-button>
                </template>
            </el-table-column>
        </el-table>
        <el-empty v-else description="暂无导出的 Perfetto Trace" />
    </el-card>
</template>

<style scoped>
.events-card {
    border-radius: 4px;
}

.trace-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
}

.trace-detail-block {
    margin: 8px 0;
}

.card-title {
    font-size: 14px;
    font-weight: 600;
    color: #303133;
}
</style>
