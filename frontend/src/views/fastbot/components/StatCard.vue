<script setup>
defineProps({
    label: { type: String, default: '' },
    value: { type: [String, Number], default: '' },
    tone: { type: String, default: '' },
})
</script>

<template>
    <el-card shadow="never" class="stat-card">
        <div class="stat-label">{{ label }}</div>
        <div class="stat-value" :class="tone">
            <slot>{{ value }}</slot>
        </div>
    </el-card>
</template>

<style scoped>
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
</style>
