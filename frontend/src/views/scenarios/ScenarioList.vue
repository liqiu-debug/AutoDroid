<script setup>
import { ref, onActivated, onDeactivated, onUnmounted, computed, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { Plus, Search, VideoPlay, Edit, Delete, Refresh, MoreFilled, 
         Check, Close, Timer, CircleClose } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import { useUserStore } from '@/stores/useUserStore'
import dayjs from 'dayjs'
import { useClientMode } from '@/composables/useClientMode'

const router = useRouter()
const userStore = useUserStore()
const { isMobileMode } = useClientMode()

// Data
const scenarios = ref([])
const loading = ref(false)
const searchQuery = ref('')
const filterStatus = ref('all') // all, success, warning, failure
const currentUser = computed(() => userStore.userInfo || {})
const isAdmin = computed(() => currentUser.value.role === 'admin')
const activeScenarioRuns = ref({})
let activeRunTimer = null

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
                const status = normalizeRunStatus(s.last_run_status) || 'not_run'
                if (filterStatus.value === 'success') return status === 'pass' || status === 'success'
                if (filterStatus.value === 'warning') return status === 'warning'
                if (filterStatus.value === 'failure') return status === 'fail' || status === 'failed'
                return true
            })
        }
        
        scenarios.value = data
        restoreActiveScenarioRuns()
        
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

const summarizeScenarioPrecheckFailure = (payload) => {
    if (!payload || typeof payload !== 'object') return '预检失败'

    const failCase = (payload.cases || []).find(item => item?.status === 'FAIL')
    if (failCase) {
        const caseName = failCase.alias || failCase.case_name || `Case#${failCase.case_id || '?'}`
        const reason = failCase.reason || '用例预检失败'
        return `${caseName}: ${reason}`
    }

    if (!payload.has_runnable_cases) return '全部用例将被跳过（当前设备无可执行步骤）'
    return '预检失败'
}

const summarizeHttpDetail = (err) => {
    const detail = err?.response?.data?.detail
    if (typeof detail === 'string' && detail) return detail
    if (detail && typeof detail === 'object') {
        if (detail.message) return detail.message
        if (Array.isArray(detail.items) && detail.items.length > 0) {
            const first = detail.items[0]
            if (first?.device_serial || first?.reason) {
                return `${first.device_serial || '未知设备'} - ${first.reason || '预检失败'}`
            }
        }
    }
    return err?.message || '请求失败'
}

const precheckScenarioOnDevice = async (scenarioId, serial) => {
    try {
        const { data } = await api.precheckScenario(scenarioId, runForm.envId, serial)
        if (data?.ok) return { ok: true }
        return { ok: false, reason: summarizeScenarioPrecheckFailure(data) }
    } catch (err) {
        return { ok: false, reason: `预检接口调用失败: ${summarizeHttpDetail(err)}` }
    }
}

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
            const firstIdle = devices.value.find(d => d.status === 'IDLE')
            if (firstIdle) {
                runForm.deviceSerials = [firstIdle.serial]
            }
        }
    } catch (err) {
        console.error('获取运行配置选项失败', err)
    }
}

const isScenarioRunActive = (row) => Boolean(activeScenarioRuns.value[row.id])

const handleRunClick = async (row) => {
    if (isScenarioRunActive(row)) {
        await terminateScenarioRun(row)
        return
    }
    runningScenarioId.value = row.id
    runDialogVisible.value = true
    fetchRunConfigOptions()
}

const confirmRun = async () => {
    if (!runningScenarioId.value) return
    if (!runForm.deviceSerials || runForm.deviceSerials.length === 0) {
        ElMessage.warning('请至少选择一台设备')
        return
    }
    try {
        const runnable = []
        const blocked = []
        for (const serial of runForm.deviceSerials) {
            const check = await precheckScenarioOnDevice(runningScenarioId.value, serial)
            if (check.ok) runnable.push(serial)
            else blocked.push({ device_serial: serial, reason: check.reason })
        }

        if (runnable.length === 0) {
            const first = blocked[0]
            ElMessage.error(`运行前预检未通过：${first ? `${first.device_serial} - ${first.reason}` : '无可执行设备'}`)
            return
        }

        const { data } = await api.runScenario(runningScenarioId.value, runForm.envId, runnable)
        const backendBlocked = Array.isArray(data?.blocked_prechecks) ? data.blocked_prechecks : []
        const allBlocked = blocked.concat(backendBlocked)
        if (allBlocked.length > 0) {
            const first = allBlocked[0]
            ElMessage.warning(`已在 ${runnable.length} 台设备启动；${allBlocked.length} 台预检失败（示例：${first.device_serial} - ${first.reason}）`)
        } else {
            ElMessage.success(`场景已在 ${runnable.length} 台设备开始批次执行`)
        }

        runDialogVisible.value = false
        // Optimistic update
        const item = scenarios.value.find(s => s.id === runningScenarioId.value)
        if (item) item.last_run_status = 'RUNNING'
        activeScenarioRuns.value = {
            ...activeScenarioRuns.value,
            [runningScenarioId.value]: {
                batch_id: data?.batch_id,
                execution_ids: data?.execution_ids || [],
                device_serials: runnable
            }
        }
        startActiveRunPolling()
        fetchScenarios() 
    } catch (err) {
        ElMessage.error('启动失败: ' + summarizeHttpDetail(err))
    }
}

const terminateScenarioRun = async (row) => {
    const active = activeScenarioRuns.value[row.id]
    if (!active) return
    try {
        await api.cancelRun({
            kind: 'scenario',
            target_id: row.id,
            batch_id: active.batch_id || null,
            execution_ids: active.execution_ids || [],
            device_serials: active.device_serials || []
        })
        ElMessage.warning('已发送终止请求')
        row.last_run_status = 'ABORTED'
        const next = { ...activeScenarioRuns.value }
        delete next[row.id]
        activeScenarioRuns.value = next
    } catch (err) {
        ElMessage.error('终止失败: ' + summarizeHttpDetail(err))
    }
}

const restoreActiveScenarioRuns = async () => {
    const runningRows = scenarios.value.filter(row => normalizeRunStatus(row.last_run_status) === 'running')
    if (runningRows.length === 0) return
    const next = { ...activeScenarioRuns.value }
    for (const row of runningRows) {
        try {
            const { data } = await api.getActiveRuns('scenario', row.id)
            const items = data?.items || []
            if (items.length > 0) {
                next[row.id] = {
                    batch_id: items[0]?.batch_id,
                    execution_ids: items.map(item => item.execution_id).filter(Boolean),
                    device_serials: items.map(item => item.device_serial).filter(Boolean)
                }
            }
        } catch {}
    }
    activeScenarioRuns.value = next
    if (Object.keys(next).length > 0) startActiveRunPolling()
}

const startActiveRunPolling = () => {
    stopActiveRunPolling()
    activeRunTimer = setInterval(async () => {
        const entries = Object.entries(activeScenarioRuns.value)
        if (entries.length === 0) {
            stopActiveRunPolling()
            return
        }
        const next = { ...activeScenarioRuns.value }
        let changed = false
        for (const [scenarioId] of entries) {
            try {
                const { data } = await api.getActiveRuns('scenario', Number(scenarioId))
                if ((data?.items || []).length === 0) {
                    delete next[scenarioId]
                    changed = true
                }
            } catch {}
        }
        activeScenarioRuns.value = next
        if (changed) fetchScenarios()
    }, 3000)
}

const stopActiveRunPolling = () => {
    if (activeRunTimer) {
        clearInterval(activeRunTimer)
        activeRunTimer = null
    }
}

const handleDelete = async (row) => {
    if (!canDeleteScenario(row)) {
        ElMessage.warning('仅创建人或管理员可以删除')
        return
    }
    try {
        await ElMessageBox.confirm(`确定删除场景 "${row.name}"?`, '警告', {
            type: 'warning',
        })
        
        loading.value = true


        await api.deleteScenario(row.id)
        ElMessage.success('删除成功')
        fetchScenarios()
    } catch (err) {
        if (err !== 'cancel') ElMessage.error('删除失败: ' + summarizeHttpDetail(err))
    } finally {
        loading.value = false
    }
}

const canDeleteScenario = (row) => {
    if (!row) return false
    if (isAdmin.value) return true
    return row.user_id !== null && row.user_id !== undefined && row.user_id === currentUser.value.id
}

const deletePermissionTip = (row) => {
    return canDeleteScenario(row) ? '删除' : '仅创建人或管理员可以删除'
}

/** 状态标签类型映射 */
const statusTagType = (status) => {
  const map = { IDLE: 'success', BUSY: 'danger', OFFLINE: 'info', WDA_DOWN: 'warning' }
  return map[status] || 'info'
}

/** 状态中文映射 */
const statusLabel = (status) => {
  const map = { IDLE: '🟢 空闲', BUSY: '🔴 执行中', OFFLINE: '⚫ 离线', WDA_DOWN: '🟠 WDA异常' }
  return map[status] || status
}

const isDeviceSelectable = (device) => device?.status === 'IDLE'

const deviceUnavailableReason = (device) => {
    if (!device) return ''
    if (device.status === 'WDA_DOWN') return 'WDA 未就绪'
    if (device.status === 'BUSY') return '设备正忙'
    return ''
}

const hasWdaDownDevice = computed(() => devices.value.some(d => d.status === 'WDA_DOWN'))

const handleReport = (row) => {
    if (row.last_execution_id) {
        // New: Go to Report Detail (Vue Router)
        router.push(`/execution/reports/${row.last_execution_id}`)
        return
    }
    
    if (row.last_report_id) {
        // Legacy: Open static HTML report
        const url = api.getReportAssetUrl(row.last_report_id)
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
    const s = normalizeRunStatus(status)
    if (s === 'pass' || s === 'success') return '#67C23A' // Green
    if (s === 'fail' || s === 'failed') return '#F56C6C' // Red
    if (s === 'warning') return '#E6A23C' // Orange
    if (s === 'running') return '#409EFF' // Blue
    if (s === 'aborted' || s === 'cancelled') return '#909399'
    return '#E6A23C' // Warning
}

const normalizeRunStatus = (status) => (status || '').toString().toLowerCase()

const getDuration = (row) => {
    if (!row.last_run_duration) return '-'
    const duration = row.last_run_duration
    if (duration < 60) return `${duration}s`
    const m = Math.floor(duration / 60)
    const s = duration % 60
    return `${m}m ${s}s`
}

onActivated(() => {
    fetchScenarios()
})

onDeactivated(stopActiveRunPolling)
onUnmounted(stopActiveRunPolling)
</script>

<template>
    <div v-if="isMobileMode" class="mobile-scenario-page">
        <div class="mobile-scenario-toolbar">
            <el-input
                v-model="searchQuery"
                placeholder="搜索场景..."
                :prefix-icon="Search"
                clearable
                class="mobile-search-input"
                @keyup.enter="handleSearch"
                @clear="handleSearch"
            />
            <el-button :icon="Refresh" circle @click="fetchScenarios" />
        </div>

        <el-radio-group v-model="filterStatus" class="mobile-status-filter" @change="handleSearch">
            <el-radio-button value="all">全部</el-radio-button>
            <el-radio-button value="success">成功</el-radio-button>
            <el-radio-button value="warning">告警</el-radio-button>
            <el-radio-button value="failure">失败</el-radio-button>
        </el-radio-group>

        <div class="mobile-scenario-list" v-loading="loading">
            <article
                v-for="item in scenarios"
                :key="item.id"
                class="mobile-scenario-card"
            >
                <div class="mobile-scenario-strip" :style="{ backgroundColor: getStatusColor(item.last_run_status) }"></div>
                <div class="mobile-scenario-body">
                    <div class="mobile-scenario-header">
                        <div class="mobile-scenario-title">
                            <strong>{{ item.name }}</strong>
                        </div>
                        <el-tag size="small" effect="plain">
                            {{ normalizeRunStatus(item.last_run_status) || 'not_run' }}
                        </el-tag>
                    </div>
                    <div class="mobile-scenario-info-line">
                        <span>{{ item.step_count || 0 }} 步</span>
                        <span>{{ getDuration(item) }}</span>
                        <span>更新 {{ formatDate(item.updated_at) }}</span>
                        <span v-if="item.last_run_time">执行 {{ formatDate(item.last_run_time) }}</span>
                        <span v-else>未执行</span>
                    </div>
                    <div class="mobile-scenario-actions">
                        <el-button type="primary" :icon="VideoPlay" @click="handleRunClick(item)">执行场景</el-button>
                        <el-button :disabled="!item.last_execution_id && !item.last_report_id" @click="handleReport(item)">查看报告</el-button>
                    </div>
                </div>
            </article>
            <el-empty v-if="!loading && scenarios.length === 0" description="暂无场景" :image-size="90" />
        </div>

        <div class="mobile-pagination" v-if="total > 0">
            <el-pagination
                v-model:current-page="currentPage"
                :page-size="pageSize"
                :background="true"
                layout="prev, pager, next"
                :total="total"
                @current-change="handleCurrentChange"
            />
        </div>
    </div>

    <div v-else class="scenario-list-container">
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
                        <el-radio-button value="all">全部</el-radio-button>
                        <el-radio-button value="success">成功</el-radio-button>
                        <el-radio-button value="warning">告警</el-radio-button>
                        <el-radio-button value="failure">失败</el-radio-button>
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
                                <template v-if="normalizeRunStatus(item.last_run_status) === 'pass' || normalizeRunStatus(item.last_run_status) === 'success'">
                                    <span class="status-text success">
                                        <el-icon><Check /></el-icon> 上次运行成功
                                    </span>
                                </template>
                                <template v-else-if="normalizeRunStatus(item.last_run_status) === 'warning'">
                                    <span class="status-text warning">
                                        <el-icon><Timer /></el-icon> 上次运行有告警: {{ item.last_failed_step || '存在忽略错误或步骤跳过' }}
                                    </span>
                                </template>
                                <template v-else-if="normalizeRunStatus(item.last_run_status) === 'fail' || normalizeRunStatus(item.last_run_status) === 'failed'">
                                    <span class="status-text failure">
                                        <el-icon><Close /></el-icon> 失败于步骤: {{ item.last_failed_step || '未知步骤' }}
                                    </span>
                                </template>
                                 <template v-else-if="normalizeRunStatus(item.last_run_status) === 'running'">
                                    <span class="status-text running">
                                        <el-icon class="is-loading"><Refresh /></el-icon> 执行中...
                                    </span>
                                </template>
                                <template v-else-if="normalizeRunStatus(item.last_run_status) === 'aborted' || normalizeRunStatus(item.last_run_status) === 'cancelled'">
                                    <span class="status-text neutral">
                                        已终止
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
                                    <el-button
                                      :type="isScenarioRunActive(item) ? 'danger' : 'primary'"
                                      :icon="isScenarioRunActive(item) ? CircleClose : VideoPlay"
                                      circle
                                      class="action-btn-run"
                                      @click="handleRunClick(item)"
                                    />
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
                                        <el-dropdown-item
                                            divided
                                            :disabled="!canDeleteScenario(item)"
                                            :style="{ color: canDeleteScenario(item) ? '#F56C6C' : '#C0C4CC' }"
                                            :title="deletePermissionTip(item)"
                                            @click="handleDelete(item)"
                                        >
                                            删除
                                        </el-dropdown-item>
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
                            :disabled="!isDeviceSelectable(dev)"
                        >
                            <div style="display: flex; justify-content: space-between; align-items: center; width: 100%;">
                                <span>{{ dev.custom_name || dev.market_name || dev.model || dev.serial }}</span>
                                <div style="display: flex; align-items: center; gap: 6px;">
                                    <el-tag :type="statusTagType(dev.status)" size="small">{{ statusLabel(dev.status) }}</el-tag>
                                    <span v-if="deviceUnavailableReason(dev)" style="font-size: 12px; color: #e6a23c;">
                                        {{ deviceUnavailableReason(dev) }}
                                    </span>
                                </div>
                            </div>
                        </el-option>
                    </el-select>
                    <div v-if="hasWdaDownDevice" class="run-warning-hint">
                        检测到 iOS 设备 WDA 异常，需在设备中心先执行“检测WDA”。
                    </div>
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

    <el-drawer
        v-if="isMobileMode"
        v-model="runDialogVisible"
        title="运行配置"
        placement="bottom"
        size="82%"
    >
        <div class="mobile-run-form">
            <label class="mobile-run-label">目标设备</label>
            <el-checkbox-group v-model="runForm.deviceSerials" class="mobile-device-checks">
                <el-checkbox
                    v-for="dev in devices"
                    :key="dev.serial"
                    :label="dev.serial"
                    :disabled="!isDeviceSelectable(dev)"
                    class="mobile-device-check"
                >
                    <div class="mobile-device-check-content">
                        <span>{{ dev.custom_name || dev.market_name || dev.model || dev.serial }}</span>
                        <el-tag :type="statusTagType(dev.status)" size="small">{{ statusLabel(dev.status) }}</el-tag>
                    </div>
                    <small v-if="deviceUnavailableReason(dev)">{{ deviceUnavailableReason(dev) }}</small>
                </el-checkbox>
            </el-checkbox-group>
            <div v-if="hasWdaDownDevice" class="run-warning-hint">
                检测到 iOS 设备 WDA 异常，需在设备中心先执行“检测WDA”。
            </div>

            <label class="mobile-run-label">运行环境</label>
            <el-select v-model="runForm.envId" placeholder="选择环境 (可选)" clearable style="width: 100%">
                <el-option
                    v-for="env in environments"
                    :key="env.id"
                    :label="env.name"
                    :value="env.id"
                />
            </el-select>
        </div>

        <template #footer>
            <div class="mobile-drawer-footer">
                <el-button @click="runDialogVisible = false">取消</el-button>
                <el-button type="primary" @click="confirmRun">开始执行</el-button>
            </div>
        </template>
    </el-drawer>
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
.status-text.warning { color: #E6A23C; }
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

.run-warning-hint {
    margin-top: 6px;
    font-size: 12px;
    color: #e6a23c;
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

.mobile-scenario-page {
    height: 100%;
    display: flex;
    flex-direction: column;
    background: #f6f7f9;
    padding: 12px;
    box-sizing: border-box;
    overflow: hidden;
}

.mobile-scenario-toolbar {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 8px;
    margin-bottom: 10px;
}

.mobile-search-input {
    width: 100%;
}

.mobile-status-filter {
    margin-bottom: 10px;
    overflow-x: auto;
    flex-shrink: 0;
}

.mobile-scenario-list {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.mobile-scenario-card {
    position: relative;
    display: flex;
    border: 1px solid #ebeef5;
    border-radius: 8px;
    background: #ffffff;
    overflow: hidden;
}

.mobile-scenario-strip {
    width: 5px;
    flex-shrink: 0;
}

.mobile-scenario-body {
    min-width: 0;
    flex: 1;
    padding: 10px 12px;
}

.mobile-scenario-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
}

.mobile-scenario-title {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
}

.mobile-scenario-title strong {
    font-size: 15px;
    color: #303133;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.mobile-scenario-info-line {
    margin-top: 6px;
    display: flex;
    align-items: center;
    gap: 7px;
    min-width: 0;
    overflow: hidden;
    font-size: 12px;
    color: #909399;
    white-space: nowrap;
}

.mobile-scenario-info-line span {
    min-width: 0;
    flex-shrink: 1;
    overflow: hidden;
    text-overflow: ellipsis;
}

.mobile-scenario-info-line span::after {
    content: "·";
    margin-left: 7px;
    color: #c0c4cc;
}

.mobile-scenario-info-line span:last-child::after {
    content: "";
    margin-left: 0;
}

.mobile-scenario-actions {
    margin-top: 9px;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
}

.mobile-scenario-actions .el-button {
    margin-left: 0;
    min-width: 0;
}

.mobile-pagination {
    padding-top: 10px;
    display: flex;
    justify-content: center;
    flex-shrink: 0;
}

.mobile-run-form {
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.mobile-run-form :deep(.el-select__wrapper),
.mobile-run-form :deep(.el-input__wrapper) {
    min-height: 44px;
    font-size: 16px;
}

.mobile-run-form :deep(.el-select__placeholder),
.mobile-run-form :deep(.el-input__inner) {
    font-size: 16px;
}

.mobile-run-label {
    font-size: 13px;
    font-weight: 600;
    color: #303133;
}

.mobile-device-checks {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.mobile-device-check {
    margin-right: 0;
    border: 1px solid #ebeef5;
    border-radius: 8px;
    padding: 10px;
    background: #fff;
}

.mobile-device-check :deep(.el-checkbox__label) {
    flex: 1;
    min-width: 0;
}

.mobile-device-check-content {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    min-width: 0;
}

.mobile-device-check-content span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.mobile-device-check small {
    display: block;
    margin-top: 4px;
    color: #e6a23c;
}

.mobile-drawer-footer {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
}

.mobile-drawer-footer .el-button {
    margin-left: 0;
}
</style>
