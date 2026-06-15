<script setup>
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Monitor, Timer, User, Picture, View } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import dayjs from 'dayjs'
import { ACTION_LABELS } from '@/utils/actionConstants'
import { useClientMode } from '@/composables/useClientMode'

const route = useRoute()
const router = useRouter()
const id = route.params.id
const { isMobileMode } = useClientMode()

// Data
const loading = ref(false)
const execution = ref(null)
const cases = ref([])
const activeCaseNames = ref([])
const showScreenshot = ref(false)
const currentScreenshot = ref('')
const currentPreviewTitle = ref('步骤预览')
const devices = ref([])

// Methods
const fetchDevices = async () => {
    try {
        const { data } = await api.getDeviceList()
        devices.value = data || []
    } catch (e) {
        console.error('获取设备列表失败:', e)
    }
}

const formatDeviceName = (deviceSerial, fallbackInfo) => {
    const dev = deviceSerial ? devices.value.find(d => d.serial === deviceSerial) : null
    if (dev) {
        const name = dev.custom_name || dev.market_name || dev.model
        if (name) return name
    }

    const serial = String(deviceSerial || '').trim()
    const info = String(fallbackInfo || '').trim()

    if (info) {
        const cleanedInfo = info.replace(/\s*\([^)]+\)$/, '').trim()
        if (cleanedInfo) return cleanedInfo
    }

    const serialLike = /^[0-9A-Za-z-]{8,}$/.test(serial)
    if (serialLike) return 'Unknown Device'
    return serial || 'Unknown Device'
}

const normalizeStatus = (status) => String(status || '').toUpperCase()

const translateStepDesc = (desc) => {
    const match = desc.match(/^(\w+)\s*(.*)$/)
    if (match && ACTION_LABELS[match[1]]) {
        return `${ACTION_LABELS[match[1]]} ${match[2]}`.trim()
    }
    return desc
}

const getStatusTagType = (status) => {
    const s = normalizeStatus(status)
    if (s === 'PASS') return 'success'
    if (s === 'WARNING') return 'warning'
    if (s === 'SKIP') return 'info'
    if (s === 'RUNNING') return ''
    if (s === 'ABORTED') return 'info'
    return 'danger'
}

const getMessageClass = (status) => {
    const s = normalizeStatus(status)
    if (s === 'WARNING') return 'warning-text'
    if (s === 'SKIP') return 'skip-text'
    return 'error-text'
}

const formatStepMessage = (row) => {
    if (!row?.error_message) return ''
    return normalizeStatus(row.status) === 'SKIP'
        ? `跳过原因: ${row.error_message}`
        : row.error_message
}

const resolvePreviewUrl = (path) => {
    const raw = String(path || '').trim()
    if (!raw) return ''
    if (raw.startsWith('data:image/')) return raw
    if (raw.startsWith('/api/static/')) return raw
    if (raw.startsWith('/static/')) return `/api${raw}`
    if (raw.startsWith('static/')) return `/api/${raw}`
    return api.getReportAssetUrl(raw)
}

const getStepPreviewPath = (row) => {
    const display = row?.report_display || {}
    return display.preview_type === 'template_image' ? (display.preview_path || '') : ''
}

const getStepPreviewLabel = (row) => {
    const display = row?.report_display || {}
    return display.preview_label || '图像预览'
}

const hasStepPreview = (row) => Boolean(getStepPreviewPath(row))

const getFailureScreenshotPath = (row) => {
    const status = normalizeStatus(row?.status)
    if (!['FAIL', 'WARNING'].includes(status)) return ''
    return row?.screenshot_path || ''
}

const hasFailureScreenshot = (row) => Boolean(getFailureScreenshotPath(row))

const fetchDetail = async () => {
    loading.value = true
    try {
        const res = await api.getReport(id)
        if (res.data) {
            execution.value = res.data
            
            // Parse flat steps into cases
            const rawSteps = res.data.steps || []
            const caseMap = new Map()
            
            rawSteps.forEach(step => {
                let caseName = "未分组步骤"
                let stepDesc = String(step.step_name || '')

                // Parse pattern: "[case_name] step_desc"
                const match = stepDesc.match(/^\[(.*?)\]\s*(.*)$/)
                if (match) {
                    caseName = match[1]
                    stepDesc = match[2]
                }

                const display = step.report_display || {}
                stepDesc = display.display_text || translateStepDesc(stepDesc)
                
                if (!caseMap.has(caseName)) {
                    caseMap.set(caseName, {
                        name: caseName,
                        status: 'PASS',
                        duration: 0,
                        steps: [],
                        hasError: false
                    })
                }
                
                const c = caseMap.get(caseName)
                c.steps.push({
                    ...step,
                    local_step_order: c.steps.length + 1,
                    display_name: stepDesc
                })
                c.duration += (step.duration || 0)

                const status = normalizeStatus(step.status)
                if (status === 'FAIL') {
                    c.status = 'FAIL'
                    c.hasError = true
                } else if (status === 'WARNING' && c.status !== 'FAIL') {
                    c.status = 'WARNING'
                } else if (status === 'SKIP' && c.status === 'PASS') {
                    c.status = 'WARNING'
                }
            })
            
            cases.value = Array.from(caseMap.values()).map((item, index) => ({
                ...item,
                collapseKey: `${index}-${item.name}`,
            }))
            activeCaseNames.value = []
        }
    } catch (err) {
        ElMessage.error('获取报告详情失败')
    } finally {
        loading.value = false
    }
}

const handleBack = () => {
    router.back()
}

const viewStepPreview = (row) => {
    const path = getStepPreviewPath(row)
    if (!path) return
    currentScreenshot.value = resolvePreviewUrl(path)
    currentPreviewTitle.value = getStepPreviewLabel(row)
    showScreenshot.value = true
}

const viewFailureScreenshot = (row) => {
    const path = getFailureScreenshotPath(row)
    if (!path) return
    currentScreenshot.value = resolvePreviewUrl(path)
    currentPreviewTitle.value = '失败截图'
    showScreenshot.value = true
}

// Helpers
const formatDate = (date) => {
    if (!date) return '-'
    return dayjs(date).format('YYYY-MM-DD HH:mm:ss')
}

const getDuration = (ms) => {
    if (!ms) return '0ms'
    if (ms < 1000) return `${Math.round(ms)}ms`
    return `${(ms / 1000).toFixed(2)}s`
}

const tableRowClassName = ({ row }) => {
    const status = normalizeStatus(row?.status)
    if (status === 'FAIL') {
        return 'error-row'
    }
    if (status === 'WARNING') return 'warning-row'
    if (status === 'SKIP') return 'skip-row'
    return ''
}

onMounted(() => {
    fetchDevices().then(() => {
        fetchDetail()
    })
})
</script>

<template>
    <div v-if="isMobileMode" class="mobile-detail-container" v-loading="loading">
        <div class="mobile-detail-header">
            <el-button link :icon="ArrowLeft" @click="handleBack">返回</el-button>
            <div v-if="execution" class="mobile-detail-title">
                <strong>{{ execution.scenario_name }}</strong>
                <span>{{ formatDeviceName(execution.device_serial, execution.device_info) }}</span>
            </div>
            <el-tag v-if="execution" :type="getStatusTagType(execution.status)">
                {{ execution.status }}
            </el-tag>
        </div>

        <div v-if="execution" class="mobile-detail-meta">
            <span><el-icon><User /></el-icon>{{ execution.executor_name || 'System' }}</span>
            <span><el-icon><Timer /></el-icon>{{ formatDate(execution.start_time) }}</span>
        </div>

        <div class="mobile-case-flow">
            <el-collapse v-model="activeCaseNames" v-if="cases.length > 0" class="mobile-case-collapse">
                <el-collapse-item
                    v-for="(caseItem, caseIndex) in cases"
                    :key="caseItem.collapseKey"
                    :name="caseItem.collapseKey"
                >
                    <template #title>
                        <div class="mobile-detail-case-header">
                            <div>
                                <span>Case {{ caseIndex + 1 }} · {{ caseItem.steps.length }} 步</span>
                                <strong>{{ caseItem.name }}</strong>
                            </div>
                            <el-tag :type="getStatusTagType(caseItem.status)" size="small">{{ caseItem.status }}</el-tag>
                        </div>
                    </template>

                    <div class="mobile-step-scroll">
                        <div class="mobile-step-list">
                            <div
                                v-for="row in caseItem.steps"
                                :key="row.id || `${caseItem.name}-${row.local_step_order}`"
                                class="mobile-step-item"
                                :class="tableRowClassName({ row })"
                            >
                                <div class="mobile-step-index">#{{ row.local_step_order || row.step_order }}</div>
                                <div class="mobile-step-main">
                                    <div class="mobile-step-title">
                                        <strong>{{ row.display_name || row.step_name }}</strong>
                                        <el-tag :type="getStatusTagType(row.status)" size="small" effect="plain">{{ row.status }}</el-tag>
                                    </div>
                                    <p v-if="row.error_message" :class="getMessageClass(row.status)">{{ formatStepMessage(row) }}</p>
                                    <div class="mobile-step-meta">
                                        <span>{{ getDuration(row.duration) }}</span>
                                        <el-button
                                            v-if="hasStepPreview(row)"
                                            type="primary"
                                            link
                                            :icon="View"
                                            @click.stop="viewStepPreview(row)"
                                        >
                                            {{ getStepPreviewLabel(row) }}
                                        </el-button>
                                        <el-button
                                            v-if="hasFailureScreenshot(row)"
                                            type="danger"
                                            link
                                            :icon="Picture"
                                            @click.stop="viewFailureScreenshot(row)"
                                        >
                                            失败截图
                                        </el-button>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </el-collapse-item>
            </el-collapse>
            <el-empty v-else-if="!loading" description="暂无用例执行数据" />
        </div>

        <el-dialog v-model="showScreenshot" :title="currentPreviewTitle" :fullscreen="true">
            <div class="screenshot-wrapper mobile-screenshot-wrapper">
                <img :src="currentScreenshot" alt="步骤预览" />
            </div>
        </el-dialog>
    </div>

    <div v-else class="detail-container" v-loading="loading">
        <!-- Header -->
        <div class="detail-header">
            <div class="header-left">
                <el-button link :icon="ArrowLeft" @click="handleBack">返回</el-button>
                <h2 v-if="execution">{{ execution.scenario_name }}</h2>
                <el-tag v-if="execution" :type="getStatusTagType(execution.status)">
                    {{ execution.status }}
                </el-tag>
            </div>
             <div class="header-right" v-if="execution">
                 <span class="meta-item"><el-icon><User /></el-icon> {{ execution.executor_name || 'System' }}</span>
                 <span class="meta-item"><el-icon><Timer /></el-icon> {{ formatDate(execution.start_time) }}</span>
                 <span class="meta-item"><el-icon><Monitor /></el-icon> {{ formatDeviceName(execution.device_serial, execution.device_info) || 'Unknown Device' }}</span>
            </div>
        </div>

        <!-- content -->
        <div class="detail-content">
             <el-collapse v-model="activeCaseNames" v-if="cases.length > 0">
                 <el-collapse-item 
                    v-for="(caseItem, index) in cases" 
                    :key="caseItem.collapseKey"
                    :name="caseItem.collapseKey"
                 >
                     <template #title>
                         <div class="case-header">
                             <div class="case-index">{{ index + 1 }}</div>
                             <div class="case-title">{{ caseItem.name }}</div>
                             <div class="case-meta">
                                 耗时: {{ getDuration(caseItem.duration) }}
                                 <el-tag :type="getStatusTagType(caseItem.status)" size="small" class="ml-2">
                                     {{ caseItem.status }}
                                 </el-tag>
                             </div>
                         </div>
                     </template>
                     
                     <div class="case-body">
                         <el-table 
                            :data="caseItem.steps" 
                            style="width: 100%" 
                            :row-class-name="tableRowClassName"
                            stripe
                         >
                             <el-table-column label="步骤" width="80" align="center">
                                 <template #default="{ row }">
                                     #{{ row.local_step_order || row.step_order }}
                                 </template>
                             </el-table-column>
                             
                             <el-table-column label="名称 / 描述" width="276">
                                 <template #default="{ row }">
                                     <div class="step-name">
                                         {{ row.display_name || row.step_name }}
                                         <el-tag v-if="normalizeStatus(row.status) === 'WARNING'" size="small" type="warning" effect="light" style="margin-left: 8px;">已忽略错误</el-tag>
                                         <el-tag v-if="normalizeStatus(row.status) === 'SKIP'" size="small" type="info" effect="light" style="margin-left: 8px;">步骤跳过</el-tag>
                                     </div>
                                     <div v-if="row.error_message" :class="getMessageClass(row.status)">{{ formatStepMessage(row) }}</div>
                                 </template>
                             </el-table-column>
                             
                             <el-table-column
                                label="预览"
                                min-width="300"
                                align="left"
                                header-align="left"
                                class-name="preview-column"
                                label-class-name="preview-column-header"
                             >
                                 <template #default="{ row }">
                                     <el-button
                                        v-if="hasStepPreview(row)"
                                        class="preview-link-btn"
                                        type="primary"
                                        link
                                        :icon="View"
                                        @click.stop="viewStepPreview(row)"
                                     >
                                        {{ getStepPreviewLabel(row) }}
                                     </el-button>
                                 </template>
                             </el-table-column>

                             <el-table-column label="耗时" width="120">
                                 <template #default="{ row }">
                                     {{ getDuration(row.duration) }}
                                 </template>
                             </el-table-column>
                             
                             <el-table-column label="状态" width="100" align="center">
                                 <template #default="{ row }">
                                     <el-tag :type="getStatusTagType(row.status)" size="small" effect="plain">
                                         {{ row.status }}
                                     </el-tag>
                                 </template>
                             </el-table-column>
                             
                             <el-table-column label="失败截图" width="100" align="center">
                                 <template #default="{ row }">
                                     <el-button
                                        v-if="hasFailureScreenshot(row)"
                                        type="danger"
                                        link
                                        :icon="Picture"
                                        @click.stop="viewFailureScreenshot(row)"
                                     >
                                        查看
                                     </el-button>
                                 </template>
                             </el-table-column>
                         </el-table>
                     </div>
                 </el-collapse-item>
             </el-collapse>
             
             <el-empty v-else description="暂无用例执行数据" />
        </div>

        <!-- Screenshot Modal -->
        <el-dialog v-model="showScreenshot" :title="currentPreviewTitle" width="80%" top="5vh">
            <div class="screenshot-wrapper">
                <img :src="currentScreenshot" alt="步骤预览" />
            </div>
        </el-dialog>
    </div>
</template>

<style scoped>
.detail-container {
    height: 100%;
    display: flex;
    flex-direction: column;
    background: #f2f3f5;
}

.detail-header {
    background: #fff;
    padding: 16px 24px;
    border-radius: 4px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin: 10px;
}

.header-left {
    display: flex;
    align-items: center;
    gap: 16px;
}

.header-left h2 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
}

.header-right {
    display: flex;
    align-items: center;
    gap: 20px;
    color: #606266;
    font-size: 13px;
}

.meta-item {
    display: flex;
    align-items: center;
    gap: 6px;
}

.detail-content {
    flex: 1;
    background: #fff;
    border-radius: 4px;
    padding: 20px;
    overflow-y: auto;
    margin: 0 10px 10px 10px;
}

.step-name {
    font-weight: 500;
}

:deep(.preview-column .cell),
:deep(.preview-column-header .cell) {
    padding-left: 0;
}

:deep(.preview-link-btn) {
    padding-left: 0;
}

.error-text {
    font-size: 12px;
    color: #F56C6C;
    margin-top: 4px;
}

.warning-text {
    font-size: 12px;
    color: #E6A23C;
    margin-top: 4px;
}

.skip-text {
    font-size: 12px;
    color: #909399;
    margin-top: 4px;
}

.screenshot-wrapper {
    display: flex;
    justify-content: center;
    background: #000;
    border-radius: 4px;
    overflow: hidden;
}

.screenshot-wrapper img {
    max-width: 100%;
    max-height: 80vh;
}

/* Case Group Styles */
:deep(.el-collapse) {
    border-top: none;
    border-bottom: none;
}

:deep(.el-collapse-item__header) {
    background: #f8f9fa;
    border-radius: 6px;
    margin-bottom: 8px;
    border-bottom: 1px solid #e9ecef;
    padding: 0 16px;
    height: 56px;
    line-height: normal;
}

:deep(.el-collapse-item__wrap) {
    border-bottom: none;
    background: transparent;
}

:deep(.el-collapse-item__content) {
    padding-bottom: 16px;
}

.case-header {
    display: flex;
    align-items: center;
    width: 100%;
    min-height: 56px;
}

.case-index {
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: #e9ecef;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 600;
    font-size: 13px;
    color: #6c757d;
    margin-right: 12px;
}

.case-title {
    font-size: 15px;
    font-weight: 600;
    color: #1a1a2e;
    flex: 1;
}

.case-meta {
    font-size: 13px;
    color: #6c757d;
    display: flex;
    align-items: center;
    align-self: center;
    gap: 8px;
    margin-right: 16px;
}

.case-meta :deep(.el-tag) {
    display: inline-flex;
    align-items: center;
}

.case-body {
    border: 1px solid #ebeef5;
    border-radius: 6px;
    overflow: hidden;
}

/* Error Row Highlight */
:deep(.el-table .error-row) {
    background-color: #fef0f0 !important;
}

:deep(.el-table .error-row:hover > td.el-table__cell) {
    background-color: #fde2e2 !important;
}

/* Warning Row Highlight */
:deep(.el-table .warning-row) {
    background-color: #fdf6ec !important;
}

:deep(.el-table .warning-row:hover > td.el-table__cell) {
    background-color: #fcf1e3 !important;
}

/* Skip Row Highlight */
:deep(.el-table .skip-row) {
    background-color: #f4f4f5 !important;
}

:deep(.el-table .skip-row:hover > td.el-table__cell) {
    background-color: #ebedef !important;
}

.mobile-detail-container {
    height: 100%;
    display: flex;
    flex-direction: column;
    background: #f6f7f9;
    overflow: hidden;
}

.mobile-detail-header {
    padding: 10px 12px;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: 8px;
    align-items: center;
    background: #ffffff;
    border-bottom: 1px solid #ebeef5;
    flex-shrink: 0;
}

.mobile-detail-title {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 3px;
}

.mobile-detail-title strong {
    font-size: 15px;
    color: #303133;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.mobile-detail-title span {
    font-size: 12px;
    color: #909399;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.mobile-detail-meta {
    padding: 8px 12px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    color: #606266;
    font-size: 12px;
    background: #ffffff;
    border-bottom: 1px solid #ebeef5;
    flex-shrink: 0;
}

.mobile-detail-meta span {
    display: flex;
    align-items: center;
    gap: 6px;
}

.mobile-case-flow {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.mobile-case-collapse {
    border-top: none;
    border-bottom: none;
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.mobile-case-collapse :deep(.el-collapse-item) {
    border: 1px solid #ebeef5;
    border-radius: 8px;
    background: #ffffff;
    overflow: hidden;
}

.mobile-case-collapse :deep(.el-collapse-item__header) {
    height: auto;
    min-height: 66px;
    margin-bottom: 0;
    padding: 0 12px;
    border-radius: 0;
    border-bottom: none;
    background: #ffffff;
}

.mobile-case-collapse :deep(.el-collapse-item.is-active .el-collapse-item__header) {
    border-bottom: 1px solid #ebeef5;
}

.mobile-case-collapse :deep(.el-collapse-item__wrap) {
    border-bottom: none;
    background: #ffffff;
}

.mobile-case-collapse :deep(.el-collapse-item__content) {
    padding: 0;
}

.mobile-detail-case {
    border: 1px solid #ebeef5;
    border-radius: 8px;
    background: #ffffff;
    overflow: hidden;
}

.mobile-detail-case-header {
    padding: 12px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    border-bottom: 1px solid #ebeef5;
}

.mobile-detail-case-header :deep(.el-tag) {
    display: inline-flex;
    align-items: center;
    flex-shrink: 0;
}

.mobile-case-collapse .mobile-detail-case-header {
    width: 100%;
    padding: 0;
    border-bottom: none;
}

.mobile-detail-case-header div {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
}

.mobile-detail-case-header span {
    font-size: 11px;
    color: #909399;
}

.mobile-detail-case-header strong {
    font-size: 14px;
    color: #303133;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.mobile-step-list {
    display: flex;
    flex-direction: column;
}

.mobile-step-scroll {
    overflow: visible;
}

.mobile-step-item {
    display: grid;
    grid-template-columns: 38px minmax(0, 1fr);
    gap: 8px;
    padding: 12px;
    border-bottom: 1px solid #f0f2f5;
}

.mobile-step-item:last-child {
    border-bottom: none;
}

.mobile-step-index {
    width: 30px;
    height: 30px;
    border-radius: 50%;
    background: #f4f4f5;
    color: #606266;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 12px;
    font-weight: 600;
}

.mobile-step-main {
    min-width: 0;
}

.mobile-step-title {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
}

.mobile-step-title strong {
    min-width: 0;
    font-size: 13px;
    color: #303133;
    line-height: 1.4;
}

.mobile-step-main p {
    margin: 6px 0 0;
    line-height: 1.5;
}

.mobile-step-meta {
    margin-top: 8px;
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    color: #909399;
    font-size: 12px;
}

.mobile-step-item.error-row {
    background: #fef0f0;
}

.mobile-step-item.warning-row {
    background: #fdf6ec;
}

.mobile-step-item.skip-row {
    background: #f4f4f5;
}

.mobile-screenshot-wrapper {
    min-height: calc(100dvh - 96px);
    align-items: center;
}
</style>
