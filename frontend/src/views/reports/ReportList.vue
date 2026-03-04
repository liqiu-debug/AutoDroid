<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { Search, Refresh, Timer, Download, Delete, View } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import dayjs from 'dayjs'

const router = useRouter()
const route = useRoute()

// ========== 顶层 Tab ==========
const activeTab = ref(route.query.tab === 'fastbot' ? 'fastbot' : 'ui')

// ========== UI 场景报告 ==========
const loading = ref(false)
const executions = ref([])
const searchQuery = ref('')
const filterStatus = ref('all')
const currentPage = ref(1)
const pageSize = ref(20)
const totalRecords = ref(0)
const devicesMap = ref({})

const fetchDevices = async () => {
    try {
        const { data } = await api.getFastbotDevices()
        const map = {}
        data.forEach(d => {
            map[d.serial] = d
        })
        devicesMap.value = map
    } catch (e) {
        console.error('Failed to fetch devices for map:', e)
    }
}

const formatDeviceName = (identifier) => {
    if (!identifier) return '未知设备'
    const dev = devicesMap.value[identifier]
    if (dev) {
        const namePart = dev.custom_name || dev.market_name || dev.model
        if (namePart) return namePart
    }
    // Handle historical data format "Model (serial)" -> "Model"
    if (typeof identifier === 'string') {
        return identifier.replace(/\s*\([^)]+\)$/, '')
    }
    return identifier
}

const fetchData = async () => {
    loading.value = true
    try {
        const params = {
            skip: (currentPage.value - 1) * pageSize.value,
            limit: pageSize.value,
        }
        if (filterStatus.value !== 'all') {
            params.status = filterStatus.value.toUpperCase()
        }
        const [reportsRes, statsRes] = await Promise.all([
            api.getReports(params),
            api.getDashboardStats()
        ])
        let data = reportsRes.data.items || []
        totalRecords.value = reportsRes.data.total || 0
        if (searchQuery.value) {
            const query = searchQuery.value.toLowerCase()
            data = data.filter(e => e.scenario_name.toLowerCase().includes(query))
        }
        const grouped = []
        const batchMap = {}
        
        data.forEach(item => {
            const bId = item.batch_id || `single_${item.id}`
            if (!batchMap[bId]) {
                batchMap[bId] = {
                    batch_id: bId,
                    batch_name: item.batch_name || item.scenario_name,
                    scenario_name: item.scenario_name,
                    start_time: item.start_time,
                    executor_name: item.executor_name,
                    status: 'RUNNING', 
                    duration: 0,
                    executions: []
                }
                grouped.push(batchMap[bId])
            }
            batchMap[bId].executions.push(item)
        })
        
        // Calculate group summary
        grouped.forEach(g => {
            let anyRunning = false
            let anyFail = false
            let maxDuration = 0
            
            g.executions.forEach(e => {
                if (e.status === 'RUNNING' || e.status === 'PENDING') anyRunning = true
                else if (e.status === 'FAIL' || e.status === 'ERROR') anyFail = true
                
                if (e.duration > maxDuration) maxDuration = e.duration
            })
            
            if (anyRunning) g.status = 'RUNNING'
            else if (anyFail) g.status = 'FAIL'
            else g.status = 'PASS'
            
            g.duration = maxDuration
        })
        
        executions.value = grouped
    } catch (err) {
        ElMessage.error('获取报告数据失败')
    } finally {
        loading.value = false
    }
}

const handleSearch = () => { currentPage.value = 1; fetchData() }
const handleSizeChange = (val) => { pageSize.value = val; currentPage.value = 1; fetchData() }
const handleCurrentChange = (val) => { currentPage.value = val; fetchData() }
const handleView = (id) => { router.push(`/execution/reports/${id}`) }
const handleDownload = (item) => { window.open(api.getReportDownloadUrl(item.id), '_blank') }

const formatDate = (date) => {
    if (!date) return '-'
    return dayjs(date).format('MM-DD HH:mm:ss')
}
const getStatusColor = (status) => {
    if (!status) return '#909399'
    const s = status.toLowerCase()
    if (s === 'pass') return '#67C23A'
    if (s === 'fail') return '#F56C6C'
    if (s === 'error') return '#E6A23C'
    if (s === 'running') return '#409EFF'
    return '#909399'
}
const getDuration = (seconds) => {
    if (!seconds) return '0s'
    if (seconds < 60) return `${Math.round(seconds)}s`
    const m = Math.floor(seconds / 60)
    const s = Math.round(seconds % 60)
    return `${m}m ${s}s`
}

// ========== 智能探索报告 ==========
const fbLoading = ref(false)
const fbTasks = ref([])
const fbSearch = ref('')
let fbPollTimer = null

const fetchFbTasks = async () => {
    try {
        const res = await api.getFastbotTasks()
        fbTasks.value = res.data || []
    } catch (err) {
        console.error('获取探索任务失败', err)
    }
}

const filteredFbTasks = computed(() => {
    const map = devicesMap.value
    let list = fbTasks.value
    if (fbSearch.value) {
        const q = fbSearch.value.toLowerCase()
        list = list.filter(item => item.package_name.toLowerCase().includes(q) || item.device_serial.toLowerCase().includes(q))
    }
    return list.map(item => {
        let name = item.device_serial
        const dev = map[item.device_serial]
        if (dev) {
            const p = dev.custom_name || dev.market_name || dev.model
            if (p) name = p
        }
        return {
            ...item,
            _resolved_device: name
        }
    })
})

const handleFbView = (taskId) => { router.push(`/special/fastbot/report/${taskId}`) }

const handleFbDelete = async (row) => {
    try {
        await ElMessageBox.confirm(`确定删除任务 #${row.id}?`, '警告', {
            type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
        })
        await api.deleteFastbotTask(row.id)
        ElMessage.success('已删除')
        fetchFbTasks()
    } catch (err) {
        if (err !== 'cancel') ElMessage.error('删除失败')
    }
}

const getFbStatusType = (status) => {
    const map = { RUNNING: '', COMPLETED: 'success', FAILED: 'danger', PENDING: 'info' }
    return map[status] || 'info'
}

const getFbDuration = (task) => {
    if (!task.started_at) return '-'
    const end = task.finished_at ? dayjs(task.finished_at) : dayjs()
    const secs = end.diff(dayjs(task.started_at), 'second')
    if (secs < 60) return `${secs}s`
    const m = Math.floor(secs / 60)
    const s = secs % 60
    return `${m}m ${s}s`
}

const handleTabChange = (tab) => {
    if (tab === 'fastbot' && fbTasks.value.length === 0) {
        fetchFbTasks()
    }
}

onMounted(() => {
    fetchDevices()
    fetchData()
    if (activeTab.value === 'fastbot') fetchFbTasks()
    fbPollTimer = setInterval(() => {
        if (activeTab.value === 'fastbot') fetchFbTasks()
    }, 15000)
})

onUnmounted(() => {
    if (fbPollTimer) clearInterval(fbPollTimer)
})
</script>

<template>
    <div class="report-container">
        <div class="content-wrapper">
            <!-- 顶层 Tab 切换 -->
            <el-tabs v-model="activeTab" class="report-tabs" @tab-change="handleTabChange">
                <el-tab-pane label="UI 场景报告" name="ui">
                    <!-- UI 报告筛选栏 -->
                    <div class="list-header">
                        <div class="left-filters">
                            <el-input
                                v-model="searchQuery"
                                placeholder="搜索场景名称..."
                                :prefix-icon="Search"
                                clearable
                                class="search-input"
                                @keyup.enter="handleSearch"
                                @clear="handleSearch"
                            />
                            <el-radio-group v-model="filterStatus" @change="handleSearch">
                                <el-radio-button label="all">全部</el-radio-button>
                                <el-radio-button label="pass">成功</el-radio-button>
                                <el-radio-button label="fail">失败</el-radio-button>
                            </el-radio-group>
                        </div>
                        <div class="right-actions">
                            <el-button :icon="Refresh" circle @click="fetchData" />
                        </div>
                    </div>

                    <!-- UI 报告列表 -->
                    <div class="list-scroll-area" v-loading="loading">
                        <el-table v-if="executions.length > 0" :data="executions" style="width: 100%" row-key="batch_id" size="large">
                            <el-table-column type="expand">
                                <template #default="{ row }">
                                    <div style="padding: 10px 40px; background: #fafafa; border-radius: 4px; margin: 5px;">
                                        <el-table :data="row.executions" :show-header="false" size="small" style="background: transparent;">
                                            <el-table-column label="设备" width="200">
                                                <template #default="{ row: subRow }">
                                                    <span style="font-family: monospace; color: #606266;">
                                                        📱 {{ formatDeviceName(subRow.device_info || subRow.device_serial) }}
                                                    </span>
                                                </template>
                                            </el-table-column>
                                            <el-table-column label="状态" width="100">
                                                <template #default="{ row: subRow }">
                                                    <el-tag :type="subRow.status === 'PASS' ? 'success' : (subRow.status === 'WARNING' ? 'warning' : (subRow.status === 'RUNNING' ? '' : 'danger'))" size="small" effect="plain">
                                                        {{ subRow.status }}
                                                    </el-tag>
                                                </template>
                                            </el-table-column>
                                            <el-table-column label="耗时" width="100">
                                                <template #default="{ row: subRow }">
                                                    <span style="font-size: 13px; color: #909399;">⏱️ {{ getDuration(subRow.duration) }}</span>
                                                </template>
                                            </el-table-column>
                                            <el-table-column label="操作" align="right">
                                                <template #default="{ row: subRow }">
                                                    <el-button link type="primary" @click="handleView(subRow.id)" size="small">查看记录单</el-button>
                                                    <el-divider direction="vertical" />
                                                    <el-button link type="primary" @click="handleDownload(subRow)" size="small">下载</el-button>
                                                </template>
                                            </el-table-column>
                                        </el-table>
                                    </div>
                                </template>
                            </el-table-column>
                            
                            <el-table-column label="运行批次 / 场景" min-width="200">
                                <template #default="{ row }">
                                    <div style="display: flex; flex-direction: column; gap: 4px;">
                                        <span style="font-weight: 600; font-size: 15px; color: #303133;">{{ row.batch_name }}</span>
                                        <span style="font-size: 12px; color: #909399;">共包含了 {{ row.executions.length }} 台设备的并发执行</span>
                                    </div>
                                </template>
                            </el-table-column>
                            
                            <el-table-column label="汇总状态" width="120">
                                <template #default="{ row }">
                                    <el-tag :type="row.status === 'PASS' ? 'success' : (row.status === 'WARNING' ? 'warning' : (row.status === 'RUNNING' ? '' : 'danger'))">
                                        {{ row.status === 'RUNNING' ? '待完成' : row.status }}
                                    </el-tag>
                                </template>
                            </el-table-column>
                            
                            <el-table-column label="开始时间" width="160">
                                <template #default="{ row }">
                                    <div style="display: flex; align-items: center; gap: 4px; color: #606266;">
                                        <el-icon><Timer /></el-icon> {{ formatDate(row.start_time) }}
                                    </div>
                                </template>
                            </el-table-column>
                            
                            <el-table-column label="最大耗时" width="120">
                                <template #default="{ row }">
                                    <span style="color: #606266;">{{ getDuration(row.duration) }}</span>
                                </template>
                            </el-table-column>
                            
                            <el-table-column label="触发人" width="120">
                                <template #default="{ row }">
                                    <span style="color: #606266;">{{ row.executor_name || '-' }}</span>
                                </template>
                            </el-table-column>
                        </el-table>
                        <el-empty v-else description="暂无测试记录" />
                    </div>

                    <div class="pagination-footer" v-if="totalRecords > 0">
                        <el-pagination
                            v-model:current-page="currentPage"
                            v-model:page-size="pageSize"
                            :page-sizes="[10, 20, 50, 100]"
                            :background="true"
                            layout="total, sizes, prev, pager, next, jumper"
                            :total="totalRecords"
                            @size-change="handleSizeChange"
                            @current-change="handleCurrentChange"
                        />
                    </div>
                </el-tab-pane>

                <el-tab-pane label="智能探索报告" name="fastbot">
                    <!-- 探索报告筛选栏 -->
                    <div class="list-header">
                        <div class="left-filters">
                            <el-input
                                v-model="fbSearch"
                                placeholder="搜索包名..."
                                :prefix-icon="Search"
                                clearable
                                class="search-input"
                            />
                        </div>
                        <div class="right-actions">
                            <el-button :icon="Refresh" circle @click="fetchFbTasks" />
                        </div>
                    </div>

                    <!-- 探索报告表格 -->
                    <el-table
                        :data="filteredFbTasks"
                        v-loading="fbLoading"
                        style="width: 100%"
                        :header-cell-style="{ background: '#f5f7fa', color: '#606266' }"
                        max-height="calc(100vh - 240px)"
                    >
                        <el-table-column prop="id" label="ID" width="60" align="center" />
                        <el-table-column label="目标包名" min-width="200">
                            <template #default="{ row }">
                                <span class="pkg-name">{{ row.package_name }}</span>
                            </template>
                        </el-table-column>
                        <el-table-column label="运行设备" width="180">
                            <template #default="{ row }">{{ row._resolved_device }}</template>
                        </el-table-column>
                        <el-table-column label="开始时间" width="140" align="center">
                            <template #default="{ row }">{{ formatDate(row.started_at) }}</template>
                        </el-table-column>
                        <el-table-column label="耗时" width="90" align="center">
                            <template #default="{ row }">{{ getFbDuration(row) }}</template>
                        </el-table-column>
                        <el-table-column label="执行人" width="130" align="center">
                            <template #default="{ row }">{{ row.executor_name || '-' }}</template>
                        </el-table-column>
                        <el-table-column label="状态" width="100" align="center">
                            <template #default="{ row }">
                                <el-tag :type="getFbStatusType(row.status)" size="small" effect="plain">{{ row.status }}</el-tag>
                            </template>
                        </el-table-column>
                        <el-table-column label="崩溃" width="70" align="center">
                            <template #default="{ row }">
                                <span :class="{ 'text-danger': row.total_crashes > 0 }">{{ row.total_crashes }}</span>
                            </template>
                        </el-table-column>
                        <el-table-column label="ANR" width="70" align="center">
                            <template #default="{ row }">
                                <span :class="{ 'text-warning': row.total_anrs > 0 }">{{ row.total_anrs }}</span>
                            </template>
                        </el-table-column>
                        <el-table-column label="操作" width="140" align="center" fixed="right">
                            <template #default="{ row }">
                                <el-tooltip v-if="row.status === 'COMPLETED'" content="查看报告" placement="top">
                                    <el-button :icon="View" link type="primary" @click="handleFbView(row.id)" />
                                </el-tooltip>
                                <el-tooltip content="删除" placement="top">
                                    <el-button :icon="Delete" link type="danger" @click="handleFbDelete(row)" />
                                </el-tooltip>
                            </template>
                        </el-table-column>
                    </el-table>
                </el-tab-pane>
            </el-tabs>
        </div>
    </div>
</template>

<style scoped>
.report-container {
    flex: 1;
    height: 0;
    display: flex;
    flex-direction: column;
    background: #f2f3f5;
    overflow: hidden;
}

.content-wrapper {
    flex: 1;
    height: 0;
    background: #fff;
    border-radius: 4px;
    display: flex;
    flex-direction: column;
    margin: 10px;
    padding: 20px;
    overflow: hidden;
}

.report-tabs {
    flex: 1;
    height: 0;
    display: flex;
    flex-direction: column;
}

.report-tabs :deep(.el-tabs__content) {
    flex: 1;
    height: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

.report-tabs :deep(.el-tab-pane) {
    flex: 1;
    height: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

.list-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
    flex-shrink: 0;
}

.left-filters {
    display: flex;
    gap: 16px;
    align-items: center;
}

.search-input {
    width: 240px;
}

.list-scroll-area {
    flex: 1;
    height: 0;
    overflow-y: auto;
}

.report-item {
    display: flex;
    height: 80px;
    background: #fff;
    border: 1px solid #ebeef5;
    border-radius: 6px;
    margin-bottom: 12px;
    align-items: center;
    cursor: pointer;
    transition: all 0.2s;
    overflow: hidden;
}

.report-item:hover {
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
    border-color: #dcdfe6;
    transform: translateY(-1px);
}

.status-strip {
    width: 6px;
    height: 100%;
    flex-shrink: 0;
}

.main-content {
    flex: 1;
    padding: 0 20px;
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.row-title {
    display: flex;
    align-items: center;
    gap: 12px;
}

.scenario-name {
    font-size: 16px;
    font-weight: 600;
    color: #303133;
}

.row-meta {
    display: flex;
    align-items: center;
    font-size: 13px;
    color: #909399;
    gap: 8px;
}

.action-area {
    padding-right: 20px;
}

.pagination-footer {
    margin-top: 16px;
    display: flex;
    justify-content: flex-end;
    padding-right: 10px;
    flex-shrink: 0;
}

.pkg-name {
    font-family: monospace;
    font-size: 13px;
    color: #303133;
}

.text-danger { color: #F56C6C; font-weight: 600; }
.text-warning { color: #E6A23C; font-weight: 600; }
</style>
