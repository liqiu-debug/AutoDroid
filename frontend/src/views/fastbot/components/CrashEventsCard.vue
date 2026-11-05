<script setup>
import { formatReplayStatus, isReplayReady } from './reportFormatters'

defineProps({
    crashEvents: { type: Array, default: () => [] },
    localReplayEnabled: { type: Boolean, default: false },
})

defineEmits(['view-log', 'view-replay'])
</script>

<template>
    <el-card shadow="never" class="events-card">
        <template #header>
            <span class="card-title">异常事件记录 ({{ crashEvents.length }})</span>
        </template>
        <el-table :data="crashEvents">
            <el-table-column label="时间" prop="time" width="120" align="center" />
            <el-table-column label="类型" width="100" align="center">
                <template #default="{ row }">
                    <el-tag :type="row.type === 'ANR' || row.type === 'OOM' ? 'warning' : 'danger'" size="small">{{ row.type }}</el-tag>
                </template>
            </el-table-column>
            <el-table-column label="日志" width="120" align="center">
                <template #default="{ row }">
                    <el-button
                        v-if="row.full_log"
                        link
                        type="primary"
                        @click="$emit('view-log', row)"
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
                        @click="$emit('view-replay', row)"
                    >
                        查看回放
                    </el-button>
                    <span v-else class="text-gray">{{ formatReplayStatus(row, localReplayEnabled) }}</span>
                </template>
            </el-table-column>
        </el-table>
    </el-card>
</template>

<style scoped>
.events-card {
    border-radius: var(--ad-radius);
}

.card-title {
    font-size: 13px;
    font-weight: 600;
    color: var(--ad-text);
}

.text-gray { color: var(--ad-muted); font-size: 13px; }

@media (max-width: 767px) {
  :deep(.el-button), :deep(.el-radio-button__inner), :deep(.el-select__wrapper), :deep(.el-collapse-item__header) { min-height: 44px; }
  :deep(.el-input__inner), :deep(.el-textarea__inner) { font-size: 16px; }
  :deep(.el-form-item__label), :deep(.el-table), :deep(.el-descriptions), :deep(.el-tabs__item), :deep(.el-collapse-item__content) { font-size: 14px; }
  :deep(.el-table__cell) { font-size: 14px; }
}
</style>
