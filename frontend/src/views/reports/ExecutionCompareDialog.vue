<script setup>
import { ref, watch, computed } from 'vue'
import { ElMessage } from 'element-plus'
import dayjs from 'dayjs'
import api from '@/api'
import {
    runStatusTagType,
    runStatusLabel,
    diffChangeTagType,
    diffChangeEffect,
    diffChangeLabel,
    diffChangeRowClass,
} from '@/utils/statusMeta'

const visible = defineModel({ type: Boolean, default: false })

const props = defineProps({
    baseId: { type: [Number, String], default: null },
    targetId: { type: [Number, String], default: null },
})

const loading = ref(false)
const result = ref(null)

const fetchCompare = async () => {
    if (!props.baseId || !props.targetId) return
    loading.value = true
    result.value = null
    try {
        const { data } = await api.compareExecutions(props.baseId, props.targetId)
        result.value = data
    } catch (err) {
        ElMessage.error('获取对比结果失败：' + (err.response?.data?.detail || err.message))
        visible.value = false
    } finally {
        loading.value = false
    }
}

watch(visible, (val) => {
    if (val) fetchCompare()
})

const summaryItems = computed(() => {
    const summary = result.value?.summary
    if (!summary) return []
    return [
        { key: 'regressed', label: '新失败', count: summary.regressed },
        { key: 'fixed', label: '已修复', count: summary.fixed },
        { key: 'still-failing', label: '持续失败', count: summary.still_failing },
        { key: 'unchanged', label: '无变化', count: summary.unchanged },
        { key: 'added', label: '新增步骤', count: summary.added },
        { key: 'removed', label: '移除步骤', count: summary.removed },
    ].filter(item => item.count > 0)
})

const formatTime = (time) => (time ? dayjs(time).format('MM-DD HH:mm:ss') : '-')

const formatSeconds = (seconds) => {
    if (seconds === null || seconds === undefined) return '-'
    const value = Number(seconds)
    if (!Number.isFinite(value)) return '-'
    if (value < 60) return `${value.toFixed(1)}s`
    const m = Math.floor(value / 60)
    const s = Math.round(value % 60)
    return `${m}m ${s}s`
}

const formatMs = (ms) => {
    if (ms === null || ms === undefined) return '-'
    const value = Number(ms)
    if (!Number.isFinite(value)) return '-'
    if (Math.abs(value) < 1000) return `${Math.round(value)}ms`
    return `${(value / 1000).toFixed(2)}s`
}

const formatDelta = (value, formatter) => {
    if (value === null || value === undefined) return ''
    const num = Number(value)
    if (!Number.isFinite(num) || num === 0) return ''
    return `${num > 0 ? '+' : '-'}${formatter(Math.abs(num))}`
}

const deviceLabel = (meta) => {
    if (!meta) return '-'
    const info = String(meta.device_info || '').replace(/\s*\([^)]+\)$/, '').trim()
    return info || meta.device_serial || '-'
}

const stepDisplayName = (row) => {
    const name = row?.step_name || ''
    const match = name.match(/^\[(.*?)\]\s*(.*)$/)
    return match ? match[2] : name
}

const stepCaseName = (row) => {
    const match = String(row?.step_name || '').match(/^\[(.*?)\]\s*/)
    return match ? match[1] : ''
}

const sideError = (side) => {
    if (!side) return ''
    return side.error_message || ''
}

const tableRowClassName = ({ row }) => diffChangeRowClass(row.change)
</script>

<template>
    <el-dialog
        v-model="visible"
        title="执行结果对比"
        width="82%"
        top="4vh"
        destroy-on-close
    >
        <div v-loading="loading" class="compare-body">
            <template v-if="result">
                <div class="compare-scenario">
                    场景：<strong>{{ result.scenario_name }}</strong>
                </div>

                <!-- 元信息对照卡 -->
                <div class="compare-meta">
                    <div class="meta-card">
                        <div class="meta-card-title">
                            基准执行 #{{ result.base.id }}
                            <el-tag :type="runStatusTagType(result.base.status)" size="small">
                                {{ runStatusLabel(result.base.status) }}
                            </el-tag>
                        </div>
                        <div class="meta-row"><span>开始时间</span>{{ formatTime(result.base.start_time) }}</div>
                        <div class="meta-row"><span>耗时</span>{{ formatSeconds(result.base.duration) }}</div>
                        <div class="meta-row"><span>设备</span>{{ deviceLabel(result.base) }}</div>
                        <div class="meta-row"><span>执行人</span>{{ result.base.executor_name || 'System' }}</div>
                    </div>
                    <div class="meta-arrow">→</div>
                    <div class="meta-card">
                        <div class="meta-card-title">
                            本次执行 #{{ result.target.id }}
                            <el-tag :type="runStatusTagType(result.target.status)" size="small">
                                {{ runStatusLabel(result.target.status) }}
                            </el-tag>
                        </div>
                        <div class="meta-row"><span>开始时间</span>{{ formatTime(result.target.start_time) }}</div>
                        <div class="meta-row">
                            <span>耗时</span>{{ formatSeconds(result.target.duration) }}
                            <em v-if="formatDelta(result.duration_delta, formatSeconds)"
                                :class="result.duration_delta > 0 ? 'delta-up' : 'delta-down'">
                                {{ formatDelta(result.duration_delta, formatSeconds) }}
                            </em>
                        </div>
                        <div class="meta-row"><span>设备</span>{{ deviceLabel(result.target) }}</div>
                        <div class="meta-row"><span>执行人</span>{{ result.target.executor_name || 'System' }}</div>
                    </div>
                </div>

                <!-- 变化汇总 -->
                <div class="compare-summary" v-if="summaryItems.length">
                    <el-tag
                        v-for="item in summaryItems"
                        :key="item.key"
                        :type="diffChangeTagType(item.key)"
                        :effect="diffChangeEffect(item.key)"
                        size="small"
                    >
                        {{ item.label }} {{ item.count }}
                    </el-tag>
                </div>

                <!-- 步骤级 diff 表 -->
                <el-table
                    :data="result.steps"
                    size="small"
                    :row-class-name="tableRowClassName"
                    :header-cell-style="{ background: '#f5f7fa', color: '#606266' }"
                >
                    <el-table-column label="#" width="52" align="center">
                        <template #default="{ row }">{{ row.step_order }}</template>
                    </el-table-column>
                    <el-table-column label="变化" width="96" align="center">
                        <template #default="{ row }">
                            <el-tag
                                :type="diffChangeTagType(row.change)"
                                :effect="diffChangeEffect(row.change)"
                                size="small"
                            >
                                {{ diffChangeLabel(row.change) }}
                            </el-tag>
                        </template>
                    </el-table-column>
                    <el-table-column label="步骤" min-width="200">
                        <template #default="{ row }">
                            <div class="step-cell">
                                <span v-if="stepCaseName(row)" class="step-case">[{{ stepCaseName(row) }}]</span>
                                {{ stepDisplayName(row) }}
                                <el-tag v-if="row.name_changed" size="small" type="warning" effect="light">名称有变更</el-tag>
                            </div>
                        </template>
                    </el-table-column>
                    <el-table-column label="基准状态" width="90" align="center">
                        <template #default="{ row }">
                            <el-tag v-if="row.base" :type="runStatusTagType(row.base.status)" size="small" effect="plain">
                                {{ row.base.status }}
                            </el-tag>
                            <span v-else class="muted">-</span>
                        </template>
                    </el-table-column>
                    <el-table-column label="本次状态" width="90" align="center">
                        <template #default="{ row }">
                            <el-tag v-if="row.target" :type="runStatusTagType(row.target.status)" size="small" effect="plain">
                                {{ row.target.status }}
                            </el-tag>
                            <span v-else class="muted">-</span>
                        </template>
                    </el-table-column>
                    <el-table-column label="耗时对比" width="170" align="center">
                        <template #default="{ row }">
                            <span class="muted">{{ row.base ? formatMs(row.base.duration) : '-' }}</span>
                            <span class="muted"> → </span>
                            <span>{{ row.target ? formatMs(row.target.duration) : '-' }}</span>
                            <em v-if="formatDelta(row.duration_delta, formatMs)"
                                :class="row.duration_delta > 0 ? 'delta-up' : 'delta-down'">
                                {{ formatDelta(row.duration_delta, formatMs) }}
                            </em>
                        </template>
                    </el-table-column>
                    <el-table-column label="错误信息对照" min-width="240">
                        <template #default="{ row }">
                            <div v-if="sideError(row.base) || sideError(row.target)" class="error-compare">
                                <div v-if="sideError(row.base)" class="error-line">
                                    <span class="error-side">基准</span>
                                    <el-tag v-if="row.base?.error_code" size="small" type="danger" effect="plain" class="error-code">
                                        {{ row.base.error_code }}
                                    </el-tag>
                                    <span class="error-text">{{ sideError(row.base) }}</span>
                                </div>
                                <div v-if="sideError(row.target)" class="error-line">
                                    <span class="error-side">本次</span>
                                    <el-tag v-if="row.target?.error_code" size="small" type="danger" effect="plain" class="error-code">
                                        {{ row.target.error_code }}
                                    </el-tag>
                                    <span class="error-text">{{ sideError(row.target) }}</span>
                                </div>
                            </div>
                            <span v-else class="muted">-</span>
                        </template>
                    </el-table-column>
                </el-table>
            </template>
        </div>
    </el-dialog>
</template>

<style scoped>
.compare-body {
    min-height: 200px;
}

.compare-scenario {
    font-size: 14px;
    color: #303133;
    margin-bottom: 12px;
}

.compare-meta {
    display: flex;
    align-items: stretch;
    gap: 12px;
    margin-bottom: 12px;
}

.meta-card {
    flex: 1;
    border: 1px solid #ebeef5;
    border-radius: 6px;
    padding: 12px 16px;
    background: #fafafa;
}

.meta-card-title {
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 600;
    color: #303133;
    margin-bottom: 8px;
}

.meta-row {
    font-size: 13px;
    color: #606266;
    line-height: 1.9;
}

.meta-row span {
    display: inline-block;
    width: 64px;
    color: #909399;
}

.meta-arrow {
    align-self: center;
    font-size: 20px;
    color: #c0c4cc;
}

.compare-summary {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 12px;
}

.step-cell {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px;
}

.step-case {
    color: #909399;
    font-size: 12px;
}

.muted { color: #909399; }

.delta-up {
    color: #F56C6C;
    font-style: normal;
    font-size: 12px;
    margin-left: 4px;
}

.delta-down {
    color: #67C23A;
    font-style: normal;
    font-size: 12px;
    margin-left: 4px;
}

.error-compare {
    display: flex;
    flex-direction: column;
    gap: 4px;
}

.error-line {
    display: flex;
    align-items: baseline;
    gap: 6px;
    font-size: 12px;
}

.error-side {
    flex-shrink: 0;
    color: #909399;
}

.error-code {
    font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
}

.error-text {
    color: #F56C6C;
    word-break: break-all;
}

/* diff 行底色：regressed 红 / fixed 绿 / still-failing 灰红 / added 蓝 / removed 灰 */
:deep(.el-table .diff-row-regressed) { background-color: #fef0f0 !important; }
:deep(.el-table .diff-row-fixed) { background-color: #f0f9eb !important; }
:deep(.el-table .diff-row-still-failing) { background-color: #faf0f0 !important; }
:deep(.el-table .diff-row-added) { background-color: #ecf5ff !important; }
:deep(.el-table .diff-row-removed) { background-color: #f4f4f5 !important; }
</style>
