<script setup>
import { ref } from 'vue'
import {
    formatDiagnosisStatus,
    formatJankReason,
    formatJankSeverity,
    formatJankSource,
    formatPercent,
} from './reportFormatters'

const props = defineProps({
    jankEvents: { type: Array, default: () => [] },
    activeJankEventTime: { type: String, default: '' },
})

defineEmits(['row-click'])

const tableRef = ref(null)

const jankEventRowClassName = ({ row }) => (
    row?.time && row.time === props.activeJankEventTime ? 'active-jank-row' : ''
)

const setCurrentRow = (row) => {
    tableRef.value?.setCurrentRow?.(row)
}

defineExpose({ setCurrentRow })
</script>

<template>
    <el-card shadow="never" class="events-card">
        <template #header>
            <span class="card-title">卡顿事件记录 ({{ jankEvents.length }})</span>
        </template>
        <el-table
            v-if="jankEvents.length > 0"
            ref="tableRef"
            :data="jankEvents"
            :header-cell-style="{ background: '#f5f7fa', color: '#606266' }"
            :row-class-name="jankEventRowClassName"
            highlight-current-row
            @row-click="$emit('row-click', $event)"
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
</template>

<style scoped>
.events-card {
    border-radius: 4px;
}

.card-title {
    font-size: 14px;
    font-weight: 600;
    color: #303133;
}

.events-card :deep(.active-jank-row) {
    --el-table-tr-bg-color: #fff3f0;
}
</style>
