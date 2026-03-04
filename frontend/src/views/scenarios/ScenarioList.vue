<script setup>
import { ref, onMounted, computed, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { Plus, Search, VideoPlay, Edit, Delete, Refresh, MoreFilled, 
         Check, Close, Timer } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import dayjs from 'dayjs'

const router = useRouter()

// Data
const scenarios = ref([])
const loading = ref(false)
const searchQuery = ref('')
const filterStatus = ref('all') // all, success, failure

// Pagination
const currentPage = ref(1)
const pageSize = ref(20)
const total = ref(0)


// Methods
const fetchScenarios = async () => {
    loading.value = true
    try {
        const params = {
            skip: (currentPage.value - 1) * pageSize.value,
            limit: pageSize.value,
            keyword: searchQuery.value || undefined
        }
        const res = await api.getScenarios(params)
        
        let data = res.data.items || []
        total.value = res.data.total || 0
        
        // Status Filter (client-side)
        if (filterStatus.value !== 'all') {
            data = data.filter(s => {
                const status = s.last_run_status?.toLowerCase() || 'not_run'
                if (filterStatus.value === 'success') return status === 'pass' || status === 'success'
                if (filterStatus.value === 'failure') return status === 'fail' || status === 'failed'
                return true
            })
        }
        
        scenarios.value = data
        
    } catch (err) {
        ElMessage.error('获取场景列表失败')
    } finally {
        loading.value = false
    }
}

const handleSearch = () => {
    currentPage.value = 1
    fetchScenarios()
}

const handleSizeChange = (val) => {
    pageSize.value = val
    currentPage.value = 1
    fetchScenarios()
}

const handleCurrentChange = (val) => {
    currentPage.value = val
    fetchScenarios()
}

const handleCreate = () => {
    router.push('/ui/scenarios/create')
}

const handleEdit = (id) => {
    router.push(`/ui/scenarios/${id}/edit`)
}

// ==================== Run Configuration ====================
const runDialogVisible = ref(false)
const runningScenarioId = ref(null)
const runForm = reactive({
    envId: null,
    deviceSerials: []
})
const environments = ref([])
const devices = ref([])

const fetchRunConfigOptions = async () => {
    try {
        const [envRes, devRes] = await Promise.all([
            api.getEnvironments(),
            api.getDeviceList()
        ])
        environments.value = envRes.data || []
        
        let devs = devRes.data || []
        if (devs.devices) devs = devs.devices
        else if (devs.items) devs = devs.items
        devices.value = Array.isArray(devs) ? devs : []
        
        if (environments.value.length > 0 && !runForm.envId) {
            runForm.envId = environments.value[0].id
        }
        if (devices.value.length > 0 && runForm.deviceSerials.length === 0) {
            runForm.deviceSerials = [devices.value[0].serial]
        }
    } catch (err) {
        console.error('获取运行配置选项失败', err)
    }
}

const handleRunClick = (row) => {
    runningScenarioId.value = row.id
    runDialogVisible.value = true
    fetchRunConfigOptions()
}

const confirmRun = async () => {
    if (!runningScenarioId.value) return
    try {
        await api.runScenario(runningScenarioId.value, runForm.envId, runForm.deviceSerials)
        ElMessage.success('场景已开始批次执行')
        runDialogVisible.value = false
        // Optimistic update
        const item = scenarios.value.find(s => s.id === runningScenarioId.value)
        if (item) item.last_run_status = 'RUNNING'
        fetchScenarios() 
    } catch (err) {
        ElMessage.error('启动失败')
    }
}

const handleDelete = async (row) => {
    try {
        await ElMessageBox.confirm(`确定删除场景 "${row.name}"?`, '警告', {
            type: 'warning',
        })
        
        loading.value = true


        await api.deleteScenario(row.id)
        ElMessage.success('删除成功')
        fetchScenarios()
    } catch (err) {
        if (err !== 'cancel') ElMessage.error('删除失败')
    } finally {
        loading.value = false
    }
}

/** 状态标签类型映射 */
const statusTagType = (status) => {
  const map = { IDLE: 'success', BUSY: 'danger', OFFLINE: 'info' }
  return map[status] || 'info'
}

/** 状态中文映射 */
const statusLabel = (status) => {
  const map = { IDLE: '🟢 空闲', BUSY: '🔴 执行中', OFFLINE: '⚫ 离线' }
  return map[status] || status
}

const handleReport = (row) => {
    if (row.last_execution_id) {
        // New: Go to Report Detail (Vue Router)
        router.push(`/execution/reports/${row.last_execution_id}`)
        return
    }
    
    if (row.last_report_id) {
        // Legacy: Open static HTML report
        const url = `/api/reports/${row.last_report_id}`
        window.open(url, '_blank')
        return
    }
    
    ElMessage.warning('该场景暂无测试报告')
}

// Helpers
const formatDate = (date) => {
    if (!date) return '-'
    const d = dayjs(date)
    if (d.isSame(dayjs(), 'day')) {
        return '今天 ' + d.format('HH:mm')
    }
    return d.format('MM-DD HH:mm')
}

const getStatusColor = (status) => {
    if (!status) return '#909399' // Gray
    const s = status.toLowerCase()
    if (s === 'pass' || s === 'success') return '#67C23A' // Green
    if (s === 'fail' || s === 'failed') return '#F56C6C' // Red
    if (s === 'running') return '#409EFF' // Blue
    return '#E6A23C' // Warning
}

const getDuration = (row) => {
    if (!row.last_run_duration) return '-'
    const duration = row.last_run_duration
    if (duration < 60) return `${duration}s`
    const m = Math.floor(duration / 60)
    const s = duration % 60
    return `${m}m ${s}s`
}

onMounted(() => {
    fetchScenarios()
})
</script>

<template>
    <div class="scenario-list-container">
        <div class="content-wrapper">
            <!-- Header -->
            <div class="list-header">
                <div class="left-filters">
                    <el-input 
                        v-model="searchQuery" 
                        placeholder="搜索场景..." 
                        :prefix-icon="Search"
                        clearable
                        class="search-input"
                        @keyup.enter="handleSearch"
                        @clear="handleSearch"
                    />
                    
                    <el-radio-group v-model="filterStatus" class="status-filter" @change="handleSearch">
                        <el-radio-button label="all">全部</el-radio-button>
                        <el-radio-button label="success">成功</el-radio-button>
                        <el-radio-button label="failure">失败</el-radio-button>
                    </el-radio-group>
                </div>
                
                <div class="right-actions">
                     <el-button :icon="Refresh" circle @click="fetchScenarios" style="margin-right: 12px" />
                     <el-button type="primary" :icon="Plus" @click="handleCreate" class="create-btn">新建场景</el-button>
                </div>
            </div>

            <!-- Scrollable List -->
            <div class="list-scroll-area" v-loading="loading">
                <template v-if="scenarios.length > 0">
                    <div 
                        v-for="item in scenarios" 
                        :key="item.id" 
                        class="scenario-item"
                    >
                        <!-- 1. Status Strip (Left) -->
                        <div class="status-strip" :style="{ backgroundColor: getStatusColor(item.last_run_status) }"></div>
                        
                        <!-- 2. Main Content (Middle) -->
                        <div class="main-content">
                            <!-- L1: Title & Steps -->
                            <div class="row-title">
                                <span class="scenario-name" @click="handleEdit(item.id)">{{ item.name }}</span>
                                <el-tag size="small" effect="plain" round class="step-badge">
                                    {{ item.step_count || 0 }} Steps
                                </el-tag>
                            </div>
                            
                            <!-- L2: Status Message -->
                            <div class="row-status">
                                <template v-if="item.last_run_status === 'PASS' || item.last_run_status === 'success'">
                                    <span class="status-text success">
                                        <el-icon><Check /></el-icon> 上次运行成功
                                    </span>
                                </template>
                                <template v-else-if="item.last_run_status === 'FAIL' || item.last_run_status === 'failed'">
                                    <span class="status-text failure">
                                        <el-icon><Close /></el-icon> 失败于步骤: {{ item.last_failed_step || '未知步骤' }}
                                    </span>
                                </template>
                                 <template v-else-if="item.last_run_status === 'RUNNING'">
                                    <span class="status-text running">
                                        <el-icon class="is-loading"><Refresh /></el-icon> 执行中...
                                    </span>
                                </template>
                                <template v-else>
                                    <span class="status-text neutral">尚未执行</span>
                                </template>
                            </div>
                            
                            <!-- L3: Meta Info -->
                            <div class="row-meta">
                                <div class="meta-block">
                                    <el-avatar :size="16" class="meta-avatar" style="background:#E6A23C">
                                        {{ (item.creator_name || 'C')[0].toUpperCase() }}
                                    </el-avatar>
                                    <span class="meta-text">{{ item.creator_name || 'Unknown' }} 创建于 {{ formatDate(item.created_at) }}</span>
                                </div>
                                <el-divider direction="vertical" />
                                <div class="meta-block">
                                    <el-avatar :size="16" class="meta-avatar" style="background:#409EFF">
                                        {{ (item.updater_name || 'U')[0].toUpperCase() }}
                                    </el-avatar>
                                    <span class="meta-text">{{ item.updater_name || 'Unknown' }} 更新于 {{ formatDate(item.updated_at) }}</span>
                                </div>
                                <el-divider direction="vertical" />
                                <div class="meta-block" v-if="item.last_run_time">
                                    <el-avatar :size="16" class="meta-avatar" style="background:#67C23A">
                                        {{ (item.last_executor || 'S')[0].toUpperCase() }}
                                    </el-avatar>
                                    <span class="meta-text">{{ item.last_executor || 'System' }} 执行于 {{ formatDate(item.last_run_time) }}</span>
                                </div>
                            </div>
                        </div>
                        
                        <!-- 3. Actions (Right) -->
                        <div class="action-area">
                            <div class="duration-badge">
                                ⏱️ {{ getDuration(item) }}
                            </div>
                            
                            <div class="btn-group">
                                 <el-tooltip content="运行" placement="top">
                                    <el-button type="primary" :icon="VideoPlay" circle class="action-btn-run" @click="handleRunClick(item)" />
                                 </el-tooltip>
                                 
                                 <el-tooltip content="编辑" placement="top">
                                    <el-button link :icon="Edit" class="action-btn-edit" @click="handleEdit(item.id)">编辑</el-button>
                                 </el-tooltip>
                                 
                                 <el-dropdown trigger="click">
                                    <span class="el-dropdown-link">
                                        <el-icon class="more-icon"><MoreFilled /></el-icon>
                                    </span>
                                    <template #dropdown>
                                      <el-dropdown-menu>
                                        <el-dropdown-item @click="handleReport(item)">查看报告</el-dropdown-item>
                                        <el-dropdown-item divided style="color: #F56C6C" @click="handleDelete(item)">删除</el-dropdown-item>
                                      </el-dropdown-menu>
                                    </template>
                                  </el-dropdown>
                            </div>
                        </div>
                    </div>
                </template>
                
                <el-empty v-else description="暂无场景" />
            </div>
            
            <div class="pagination-footer" v-if="total > 0">
                <el-pagination
                  v-model:current-page="currentPage"
                  v-model:page-size="pageSize"
                  :page-sizes="[10, 20, 50, 100]"
                  :background="true"
                  layout="total, sizes, prev, pager, next, jumper"
                  :total="total"
                  @size-change="handleSizeChange"
                  @current-change="handleCurrentChange"
                />
            </div>
        </div>

        <!-- Run Configuration Dialog -->
        <el-dialog v-model="runDialogVisible" title="运行配置" width="400px">
            <el-form :model="runForm" label-width="100px">
                <el-form-item label="目标设备">
                    <el-select v-model="runForm.deviceSerials" multiple collapse-tags placeholder="选择设备 (可选)" clearable style="width: 100%">
                        <el-option
                            v-for="dev in devices"
                            :key="dev.serial"
                            :label="dev.custom_name || dev.market_name || dev.model || dev.serial"
                            :value="dev.serial"
                            :disabled="dev.status !== 'IDLE'"
                        >
                            <div style="display: flex; justify-content: space-between; align-items: center; width: 100%;">
                                <span>{{ dev.custom_name || dev.market_name || dev.model || dev.serial }}</span>
                                <el-tag :type="statusTagType(dev.status)" size="small">{{ statusLabel(dev.status) }}</el-tag>
                            </div>
                        </el-option>
                    </el-select>
                </el-form-item>
                <el-form-item label="运行环境">
                    <el-select v-model="runForm.envId" placeholder="选择环境 (可选)" clearable style="width: 100%">
                        <el-option
                            v-for="env in environments"
                            :key="env.id"
                            :label="env.name"
                            :value="env.id"
                        />
                    </el-select>
                </el-form-item>
            </el-form>
            <template #footer>
                <div class="dialog-footer">
                    <el-button @click="runDialogVisible = false">取消</el-button>
                    <el-button type="primary" @click="confirmRun">开始执行</el-button>
                </div>
            </template>
        </el-dialog>
    </div>
</template>

<style scoped>
.scenario-list-container {
    height: 100%;
    display: flex;
    flex-direction: column;
    background: #f2f3f5;
}

.content-wrapper {
    flex: 1;
    background: #fff;
    border-radius: 4px;
    display: flex;
    flex-direction: column;
    margin: 10px;
    padding: 20px;
    overflow: hidden;
}

/* Header */
.list-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;
    background: transparent;
}

.left-filters {
    display: flex;
    gap: 16px;
    align-items: center;
}

.search-input {
    width: 240px;
}

.create-btn {
    padding: 8px 20px;
    font-weight: 500;
}

/* Scroll Area */
.list-scroll-area {
    flex: 1;
    overflow-y: auto;
    /* padding-bottom: 20px; removed as wrapper handles padding */
}

/* Scenario Item Card */
.scenario-item {
    display: flex;
    height: 90px;
    background: #ffffff; /* Maintained white for items inside (card in card is fine, or maybe make items simpler?) CaseList uses table rows. Here we use cards. */
    /* Let's keep cards but make them stand out less or change background of list area? 
       Actually, if background is white, cards should have border or different bg?
       CaseList has white bg and table rows.
       Here we have cards. 
       Let's keep cards but add border. 
    */
    background: #fff;
    border-radius: 6px;
    margin-bottom: 12px;
    border: 1px solid #ebeef5;
    position: relative;
    overflow: hidden; /* For status strip */
    transition: all 0.2s ease;
    align-items: center;
}

.scenario-item:hover {
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
    border-color: #dcdfe6;
    transform: translateY(-1px);
}

/* 1. Status Strip */
.status-strip {
    width: 6px;
    height: 100%;
    flex-shrink: 0;
}

/* 2. Main Content */
.main-content {
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: center;
    padding: 0 16px;
    gap: 6px;
}

.row-title {
    display: flex;
    align-items: center;
    gap: 10px;
}

.scenario-name {
    font-size: 16px;
    font-weight: 600;
    color: #303133;
    cursor: pointer;
}
.scenario-name:hover {
    color: #409EFF;
}

.step-badge {
    font-weight: normal;
    color: #909399;
    border-color: #e4e7ed;
    background: #f4f4f5;
}

.row-status {
    font-size: 13px;
    display: flex;
    align-items: center;
}

.status-text {
    display: flex;
    align-items: center;
    gap: 4px;
    font-weight: 500;
}
.status-text.success { color: #67C23A; }
.status-text.failure { color: #F56C6C; }
.status-text.running { color: #409EFF; }
.status-text.neutral { color: #909399; }

.row-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: #909399;
}

.meta-block {
    display: flex;
    align-items: center;
    gap: 6px;
}
.meta-avatar {
    font-size: 8px; /* For text avatars */
}

/* 3. Action Area */
.action-area {
    width: 200px;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    justify-content: center;
    padding-right: 20px;
    gap: 10px;
    border-left: 1px solid #f2f6fc; /* Subtle separator */
    height: 70%;
}

.duration-badge {
    font-size: 12px;
    color: #909399;
    background: #f4f4f5;
    padding: 2px 8px;
    border-radius: 10px;
}

.btn-group {
    display: flex;
    align-items: center;
    gap: 12px;
}

.action-btn-run {
    width: 36px;
    height: 36px;
    font-size: 16px;
}

.action-btn-edit {
    font-size: 14px;
    color: #606266;
}
.action-btn-edit:hover {
    color: #409EFF;
}

.more-icon {
    font-size: 16px;
    color: #909399;
    cursor: pointer;
    padding: 4px;
    transform: rotate(90deg);
}
.more-icon:hover {
    color: #409EFF;
}

/* Custom Scrollbar for list area */
.list-scroll-area::-webkit-scrollbar {
    width: 6px;
}
.list-scroll-area::-webkit-scrollbar-thumb {
    background: #dcdfe6;
    border-radius: 4px;
}
.list-scroll-area::-webkit-scrollbar-track {
    background: transparent;
}

.pagination-footer {
  margin-top: 20px;
  display: flex;
  justify-content: flex-end;
  padding-right: 10px;
}
</style>
