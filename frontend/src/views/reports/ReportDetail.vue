<script setup>
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Monitor, Timer, User, Picture } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import dayjs from 'dayjs'

const route = useRoute()
const router = useRouter()
const id = route.params.id

// Data
const loading = ref(false)
const execution = ref(null)
const cases = ref([])
const activeCaseNames = ref([])
const showScreenshot = ref(false)
const currentScreenshot = ref('')
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

const formatDeviceName = (serial_or_info) => {
    if (!serial_or_info) return 'Unknown'
    const dev = devices.value.find(d => serial_or_info.includes(d.serial))
    if (dev) {
        const name = dev.custom_name || dev.market_name || dev.model
        if (name) return name
    }
    
    if (typeof serial_or_info === 'string') {
        return serial_or_info.replace(/\s*\([^)]+\)$/, '')
    }
    return serial_or_info
}

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
                let stepDesc = step.step_name
                
                // Parse pattern: "[case_name] step_desc"
                const match = step.step_name.match(/^\[(.*?)\]\s*(.*)$/)
                if (match) {
                    caseName = match[1]
                    stepDesc = match[2]
                }
                
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
                    display_name: stepDesc
                })
                c.duration += (step.duration || 0)
                
                if (step.status === 'FAIL') {
                    c.status = 'FAIL'
                    c.hasError = true
                }
            })
            
            cases.value = Array.from(caseMap.values())
            
            // Auto expand failing cases
            const failingCases = cases.value.filter(c => c.hasError).map(c => c.name)
            if (failingCases.length > 0) {
                activeCaseNames.value = failingCases
                
                // Scroll to the first error after DOM update
                setTimeout(() => {
                    const firstErrorEl = document.querySelector('.error-row')
                    const container = document.querySelector('.detail-content')
                    if (firstErrorEl && container) {
                        const containerRect = container.getBoundingClientRect()
                        const elRect = firstErrorEl.getBoundingClientRect()
                        container.scrollTo({
                            top: container.scrollTop + (elRect.top - containerRect.top) - 80,
                            behavior: 'smooth'
                        })
                    }
                }, 300)
            }
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

const viewScreenshot = (path) => {
    if (!path) return
    currentScreenshot.value = `/api/reports/${path}` 
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
    if (row.status !== 'PASS') {
        return 'error-row'
    }
    return ''
}

onMounted(() => {
    fetchDevices().then(() => {
        fetchDetail()
    })
})
</script>

<template>
    <div class="detail-container" v-loading="loading">
        <!-- Header -->
        <div class="detail-header">
            <div class="header-left">
                <el-button link :icon="ArrowLeft" @click="handleBack">返回</el-button>
                <h2 v-if="execution">{{ execution.scenario_name }}</h2>
                <el-tag v-if="execution" :type="execution.status === 'PASS' ? 'success' : (execution.status === 'WARNING' ? 'warning' : 'danger')">
                    {{ execution.status }}
                </el-tag>
            </div>
             <div class="header-right" v-if="execution">
                 <span class="meta-item"><el-icon><User /></el-icon> {{ execution.executor_name || 'System' }}</span>
                 <span class="meta-item"><el-icon><Timer /></el-icon> {{ formatDate(execution.start_time) }}</span>
                 <span class="meta-item"><el-icon><Monitor /></el-icon> {{ formatDeviceName(execution.device_info) || 'Unknown Device' }}</span>
            </div>
        </div>

        <!-- content -->
        <div class="detail-content">
             <el-collapse v-model="activeCaseNames" v-if="cases.length > 0">
                 <el-collapse-item 
                    v-for="(caseItem, index) in cases" 
                    :key="caseItem.name" 
                    :name="caseItem.name"
                 >
                     <template #title>
                         <div class="case-header">
                             <div class="case-index">{{ index + 1 }}</div>
                             <div class="case-title">{{ caseItem.name }}</div>
                             <div class="case-meta">
                                 耗时: {{ getDuration(caseItem.duration) }}
                                 <el-tag :type="caseItem.status === 'PASS' ? 'success' : (caseItem.status === 'WARNING' ? 'warning' : 'danger')" size="small" class="ml-2">
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
                                     #{{ row.step_order }}
                                 </template>
                             </el-table-column>
                             
                             <el-table-column label="名称 / 描述" min-width="300">
                                 <template #default="{ row }">
                                     <div class="step-name">
                                         {{ row.display_name || row.step_name }}
                                         <el-tag v-if="row.status === 'WARNING'" size="small" type="warning" effect="light" style="margin-left: 8px;">已忽略错误</el-tag>
                                     </div>
                                     <div v-if="row.error_message" :class="row.status === 'WARNING' ? 'warning-text' : 'error-text'">{{ row.error_message }}</div>
                                 </template>
                             </el-table-column>
                             
                             <el-table-column label="耗时" width="120">
                                 <template #default="{ row }">
                                     {{ getDuration(row.duration) }}
                                 </template>
                             </el-table-column>
                             
                             <el-table-column label="状态" width="100" align="center">
                                 <template #default="{ row }">
                                     <el-tag :type="row.status === 'PASS' ? 'success' : (row.status === 'WARNING' ? 'warning' : 'danger')" size="small" effect="plain">
                                         {{ row.status }}
                                     </el-tag>
                                 </template>
                             </el-table-column>
                             
                             <el-table-column label="截图" width="100" align="center">
                                 <template #default="{ row }">
                                     <el-button 
                                        v-if="row.screenshot_path" 
                                        type="primary" 
                                        link 
                                        :icon="Picture" 
                                        @click.stop="viewScreenshot(row.screenshot_path)"
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
        <el-dialog v-model="showScreenshot" title="步骤截图" width="80%" top="5vh">
            <div class="screenshot-wrapper">
                <img :src="currentScreenshot" alt="Screenshot" />
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
    gap: 8px;
    margin-right: 16px;
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
</style>
