<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'

const props = defineProps({
    modelValue: Boolean,
    // { type: 'case'|'scenario'|'batch', caseId?, scenarioId?, caseIds?, folderId?, name? }
    target: { type: Object, default: null },
    envId: { type: [Number, String], default: null },
    deviceSerial: { type: String, default: "" },
    platform: { type: String, default: "" },
})
const emit = defineEmits(['update:modelValue'])

const visible = computed({
    get: () => props.modelValue,
    set: value => emit('update:modelValue', value),
})

const loading = ref(false)
const exporting = ref(false)
const creating = ref(false)
const precheck = ref(null)
const format = ref('zip')
const ignoreErrors = ref(false)
const taskCreated = ref(null)

const targetType = computed(() => props.target?.type || 'case')
const isBatch = computed(() => targetType.value === 'batch')
const targetLabel = computed(() => ({ case: '用例', scenario: '场景', batch: '批量用例' }[targetType.value] || '用例'))
const issues = computed(() => precheck.value?.issues || [])
const errorIssues = computed(() => issues.value.filter(item => item.severity === 'error'))
const warningIssues = computed(() => issues.value.filter(item => item.severity === 'warning'))
const precheckBlocked = computed(() => precheck.value && precheck.value.ok === false && !ignoreErrors.value)
const canExport = computed(() => !loading.value && !precheckBlocked.value && !exporting.value)

const loadPrecheck = async () => {
    precheck.value = null
    ignoreErrors.value = false
    taskCreated.value = null
    if (!props.target || isBatch.value) return
    loading.value = true
    try {
        if (targetType.value === 'case') {
            const { data } = await api.autojs.precheckCase(props.target.caseId, props.envId, (props.platform || props.target?.platform || "android"))
            precheck.value = data
        } else if (targetType.value === 'scenario') {
            const { data } = await api.autojs.precheckScenario(props.target.scenarioId, props.envId, (props.platform || props.target?.platform || "android"))
            precheck.value = data
        }
    } catch (err) {
        precheck.value = {
            ok: false,
            issues: [{
                severity: 'error',
                message: '预检请求失败: ' + (err.response?.data?.detail || err.message || '未知错误'),
            }],
        }
    } finally {
        loading.value = false
    }
}

watch(() => props.modelValue, value => {
    if (value) loadPrecheck()
})

const downloadBlobResponse = (response, fallbackName) => {
    const disposition = response.headers?.['content-disposition'] || ''
    let filename = fallbackName
    const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i)
    const plainMatch = disposition.match(/filename="?([^";]+)"?/i)
    const matched = utf8Match || plainMatch
    if (matched) {
        try { filename = decodeURIComponent(matched[1]) } catch { filename = matched[1] }
    }
    const url = URL.createObjectURL(new Blob([response.data]))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename
    anchor.click()
    URL.revokeObjectURL(url)
}

const handleDownload = async () => {
    if (!props.target || exporting.value) return
    exporting.value = true
    try {
        let response
        if (targetType.value === 'case') {
            response = await api.autojs.exportCase(props.target.caseId, format.value, props.envId, props.platform || props.target?.platform || "android")
        } else if (targetType.value === 'scenario') {
            response = await api.autojs.exportScenario(props.target.scenarioId, props.envId, props.platform || props.target?.platform || "android")
        } else {
            response = await api.autojs.exportBatch({
                case_ids: props.target.caseIds || [],
                folder_id: props.target.folderId || null,
                name: props.target.name || null,
                env_id: props.envId || null,
            })
        }
        downloadBlobResponse(response, `autodroid_${targetType.value}.zip`)
        ElMessage.success('脚本包已开始下载')
    } catch (err) {
        ElMessage.error('导出失败: ' + (err.response?.data?.detail?.message || err.response?.data?.detail || err.message))
    } finally {
        exporting.value = false
    }
}

const handleCreateTask = async () => {
    if (!props.target || creating.value) return
    creating.value = true
    try {
        const payload = {
            kind: targetType.value,
            case_id: targetType.value === 'case' ? props.target.caseId : null,
            scenario_id: targetType.value === 'scenario' ? props.target.scenarioId : null,
            env_id: props.envId || null,
            name: props.target.name || null,
            device_serial: props.deviceSerial || props.target.device_serial || null,
            platform: props.platform || props.target.platform || null,
        }
        const { data } = await api.autojs.createTask(payload)
        taskCreated.value = data
        ElMessage.success('任务已创建，设备在线时会自动领取执行')
    } catch (err) {
        const detail = err.response?.data?.detail
        const message = typeof detail === 'object' ? (detail.message || '预检未通过') : (detail || err.message)
        ElMessage.error('创建任务失败: ' + message)
    } finally {
        creating.value = false
    }
}
</script>

<template>
    <el-dialog v-model="visible" :title="`导出 AutoDroid 脚本 · ${targetLabel}`" width="560px">
        <div v-loading="loading" class="export-dialog-body">
            <el-alert
                type="info"
                :closable="false"
                show-icon
                class="target-alert"
                :title="target?.name ? `目标：${target.name}` : `目标：${targetLabel}`"
                description="导出的脚本包可在安装了 AutoDroid 的 Android 设备上离线执行，执行结果自动回传平台并发送飞书通知。"
            />

            <template v-if="!isBatch">
                <div v-if="errorIssues.length" class="issue-block">
                    <el-alert type="error" :closable="false" show-icon :title="`预检发现 ${errorIssues.length} 个问题（将导致导出脚本无法执行）`" />
                    <ul class="issue-list">
                        <li v-for="(issue, index) in errorIssues" :key="'e' + index">
                            <span v-if="issue.step_order" class="issue-step">步骤 {{ issue.step_order }}</span>
                            {{ issue.message }}
                        </li>
                    </ul>
                    <el-checkbox v-model="ignoreErrors">忽略预检错误，仍然导出（不推荐）</el-checkbox>
                </div>
                <div v-if="warningIssues.length" class="issue-block">
                    <el-alert type="warning" :closable="false" show-icon :title="`${warningIssues.length} 条提示`" />
                    <ul class="issue-list">
                        <li v-for="(issue, index) in warningIssues" :key="'w' + index">
                            <span v-if="issue.step_order" class="issue-step">步骤 {{ issue.step_order }}</span>
                            {{ issue.message }}
                        </li>
                    </ul>
                </div>
                <el-alert
                    v-if="precheck && precheck.ok && !errorIssues.length && !warningIssues.length"
                    type="success"
                    :closable="false"
                    show-icon
                    title="预检通过，全部步骤可导出"
                />
            </template>
            <el-alert v-else type="info" :closable="false" show-icon title="批量导出不执行逐用例预检；如有 xpath 等不兼容步骤，会在设备端执行时失败。" />

            <div v-if="targetType === 'case'" class="format-block">
                <div class="format-label">导出格式</div>
                <el-radio-group v-model="format">
                    <el-radio value="zip">zip 脚本包（推荐，含模板图/运行时）</el-radio>
                    <el-radio value="file">单文件 .js（仅无图像步骤的简单用例）</el-radio>
                </el-radio-group>
            </div>

            <el-alert
                v-if="taskCreated"
                type="success"
                :closable="false"
                show-icon
                :title="`任务已创建：${taskCreated.task_id}`"
                description="执行结果可在「报告中心」查看；设备在线时会自动领取任务并回传结果。"
            />
        </div>
        <template #footer>
            <span class="dialog-footer">
                <el-button @click="visible = false">关闭</el-button>
                <el-button :loading="exporting" :disabled="!canExport" @click="handleDownload">下载脚本包</el-button>
                <el-button
                    v-if="!isBatch"
                    type="primary"
                    :loading="creating"
                    :disabled="!canExport || loading"
                    @click="handleCreateTask"
                >下发到设备</el-button>
            </span>
        </template>
    </el-dialog>
</template>

<style scoped>
.export-dialog-body { display: flex; flex-direction: column; gap: 12px; min-height: 120px; }
.target-alert :deep(.el-alert__description) { margin-top: 4px; }
.issue-block { display: flex; flex-direction: column; gap: 6px; }
.issue-list { margin: 0; padding-left: 18px; max-height: 160px; overflow-y: auto; font-size: 12px; color: var(--ad-text); }
.issue-step { color: var(--ad-muted); margin-right: 6px; }
.format-block { display: flex; flex-direction: column; gap: 6px; }
.format-label { font-size: 13px; color: var(--ad-text); font-weight: 600; }
</style>
