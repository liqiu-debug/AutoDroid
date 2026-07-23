<script setup>
import { ref, onActivated, onDeactivated, onMounted, onUnmounted, computed, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { Search, Refresh, Timer, Download, Delete, View, CircleClose, TrendCharts, MoreFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import dayjs from 'dayjs'
import { useClientMode } from '@/composables/useClientMode'
import { runStatusTagType } from '@/utils/statusMeta'
import { useUserStore } from '@/stores/useUserStore'
import { compatibilityExecutionMode, packageSnapshotLabel } from '@/utils/compatibilityReplay'
import {
    inspectionRunCoverage,
    inspectionRunReplay,
    inspectionRunStatusLabel,
} from '@/utils/inspectionRunPresentation'
import FlakyAnalysisDrawer from './FlakyAnalysisDrawer.vue'

const router = useRouter()
const route = useRoute()
const { isMobileMode } = useClientMode()
const userStore = useUserStore()

// ========== 顶层 Tab ==========
const resolveTab = (tab) => (['fastbot', 'startup', 'compatibility', 'inspection'].includes(tab) ? tab : 'ui')
const inspectionEnabled = computed(() => userStore.featureFlags?.model_inspection === true)
const resolveAvailableTab = tab => {
    const resolved = resolveTab(tab)
    return resolved === 'inspection' && !inspectionEnabled.value ? 'ui' : resolved
}
const activeTab = ref(resolveAvailableTab(route.query.tab))
const syncReportTabQuery = tab => {
    const queryTab = tab === 'ui' ? undefined : tab
    if (String(route.query.tab || '') === String(queryTab || '')) return
    router.replace({ query: { ...route.query, tab: queryTab } })
}
const mobileReportOptions = computed(() => [
    { label: 'UI 自动化', value: 'ui' },
    ...(inspectionEnabled.value ? [{ label: '智能巡检', value: 'inspection' }] : []),
])
const mobileReportTab = computed({
    get: () => activeTab.value === 'inspection' && inspectionEnabled.value ? 'inspection' : 'ui',
    set: (value) => {
        const next = resolveAvailableTab(value)
        activeTab.value = next
        syncReportTabQuery(next)
        handleTabChange(next)
    },
})

// ========== UI 场景报告 ==========
const loading = ref(false)
const executions = ref([])
const searchQuery = ref('')
const filterStatus = ref('all')
const currentPage = ref(1)
const pageSize = ref(20)
const totalRecords = ref(0)
const devicesMap = ref({})
const stoppingDeviceSerial = ref('')
const showFlakyDrawer = ref(false)

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

const formatDeviceName = (deviceSerial, fallbackInfo) => {
    const dev = deviceSerial ? devicesMap.value[deviceSerial] : null
    if (dev) {
        const namePart = dev.custom_name || dev.market_name || dev.model
        if (namePart) return namePart
    }

    const serial = String(deviceSerial || '').trim()
    const info = String(fallbackInfo || '').trim()

    // Handle historical data format "Model (serial)" -> "Model"
    if (info) {
        const cleanedInfo = info.replace(/\s*\([^)]+\)$/, '').trim()
        if (cleanedInfo) return cleanedInfo
    }

    // iOS UDID / serial-like fallback should not be shown in report center.
    const serialLike = /^[0-9A-Za-z-]{8,}$/.test(serial)
    if (serialLike) return '未知设备'
    return serial || '未知设备'
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
        if (searchQuery.value) {
            params.keyword = searchQuery.value
        }
        const reportsRes = await api.getReports(params)
        const data = reportsRes.data.items || []
        totalRecords.value = reportsRes.data.total || 0
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
            let anyWarning = false
            let anyAborted = false
            let maxDuration = 0
            
            g.executions.forEach(e => {
                if (e.status === 'RUNNING' || e.status === 'PENDING') anyRunning = true
                else if (e.status === 'FAIL' || e.status === 'ERROR') anyFail = true
                else if (e.status === 'WARNING') anyWarning = true
                else if (e.status === 'ABORTED') anyAborted = true
                
                if (e.duration > maxDuration) maxDuration = e.duration
            })
            
            if (anyRunning) g.status = 'RUNNING'
            else if (anyFail) g.status = 'FAIL'
            else if (anyWarning) g.status = 'WARNING'
            else if (anyAborted) g.status = 'ABORTED'
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

const isExecutionRunning = (status) => {
    const normalized = String(status || '').toUpperCase()
    return normalized === 'RUNNING' || normalized === 'PENDING'
}

const handleStopExecutionFromReport = async (item) => {
    if (!item?.device_serial) {
        ElMessage.warning('该执行记录缺少设备信息，无法停止')
        return
    }
    try {
        await ElMessageBox.confirm(
            `确定要停止 ${formatDeviceName(item.device_serial, item.device_info)} 当前执行吗？`,
            '停止当前设备执行',
            { type: 'warning', confirmButtonText: '停止执行', cancelButtonText: '取消' }
        )
        stoppingDeviceSerial.value = item.device_serial
        const { data } = await api.stopDeviceExecution(item.device_serial)
        const count = Number(data?.recovered_executions || 0)
        ElMessage.success(count > 0 ? `已停止 ${count} 条运行中执行` : '已发送停止指令')
        await Promise.all([fetchDevices(), fetchData()])
    } catch (err) {
        if (!['cancel', 'close'].includes(err)) {
            ElMessage.error('停止失败：' + (err.response?.data?.detail || err.message))
        }
    } finally {
        stoppingDeviceSerial.value = ''
    }
}

const handleDeleteExecution = async (executionId) => {
    try {
        await ElMessageBox.confirm('确定删除该条执行记录？相关截图和报告文件也将被删除。', '警告', {
            type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
        })
        await api.deleteExecution(executionId)
        ElMessage.success('已删除')
        fetchData()
    } catch (err) {
        if (err !== 'cancel') ElMessage.error('删除失败')
    }
}

const handleDeleteBatch = async (row) => {
    if (row.batch_id.startsWith('single_')) {
        return handleDeleteExecution(row.executions[0].id)
    }
    try {
        await ElMessageBox.confirm(
            `确定删除批次「${row.batch_name}」的所有执行记录（${row.executions.length} 条）？相关截图和报告文件也将被删除。`,
            '警告',
            { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
        )
        await api.deleteBatch(row.batch_id)
        ElMessage.success('已删除')
        fetchData()
    } catch (err) {
        if (err !== 'cancel') ElMessage.error('删除失败')
    }
}

const formatDate = (date) => {
    if (!date) return '-'
    return dayjs(date).format('MM-DD HH:mm:ss')
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
const fbPage = ref(1)
const fbPageSize = ref(20)
const fbTotal = ref(0)
const startupLoading = ref(false)
const startupTasks = ref([])
const startupSearch = ref('')
const startupPage = ref(1)
const startupPageSize = ref(20)
const startupTotal = ref(0)
const compatibilityLoading = ref(false)
const compatibilityRuns = ref([])
const compatibilitySearch = ref('')
const compatibilityStatus = ref('all')
const compatibilityPage = ref(1)
const compatibilityPageSize = ref(20)
const compatibilityTotal = ref(0)
const inspectionLoading = ref(false)
const inspectionRuns = ref([])
const inspectionSearch = ref('')
const inspectionStatus = ref('all')
const inspectionPage = ref(1)
const inspectionPageSize = ref(20)
const inspectionTotal = ref(0)
const inspectionDeletingId = ref(null)
let fbPollTimer = null
let pageActive = false

const fetchFbTasks = async () => {
    fbLoading.value = true
    try {
        const params = {
            skip: (fbPage.value - 1) * fbPageSize.value,
            limit: fbPageSize.value,
        }
        if (fbSearch.value) params.keyword = fbSearch.value
        const res = await api.getFastbotTasks(params)
        fbTasks.value = res.data.items || []
        fbTotal.value = res.data.total || 0
    } catch (err) {
        console.error('获取探索任务失败', err)
    } finally {
        fbLoading.value = false
    }
}

const handleFbSearch = () => { fbPage.value = 1; fetchFbTasks() }
const handleFbPageChange = (val) => { fbPage.value = val; fetchFbTasks() }
const handleFbSizeChange = (val) => { fbPageSize.value = val; fbPage.value = 1; fetchFbTasks() }

const fetchStartupTasks = async () => {
    startupLoading.value = true
    try {
        const params = {
            skip: (startupPage.value - 1) * startupPageSize.value,
            limit: startupPageSize.value,
        }
        if (startupSearch.value) params.keyword = startupSearch.value
        const res = await api.getStartupTasks(params)
        startupTasks.value = res.data.items || []
        startupTotal.value = res.data.total || 0
    } catch (err) {
        console.error('获取冷热启动任务失败', err)
    } finally {
        startupLoading.value = false
    }
}

const handleStartupSearch = () => { startupPage.value = 1; fetchStartupTasks() }
const handleStartupPageChange = (val) => { startupPage.value = val; fetchStartupTasks() }
const handleStartupSizeChange = (val) => { startupPageSize.value = val; startupPage.value = 1; fetchStartupTasks() }

const fetchCompatibilityRuns = async () => {
    compatibilityLoading.value = true
    try {
        const params = {
            skip: (compatibilityPage.value - 1) * compatibilityPageSize.value,
            limit: compatibilityPageSize.value,
        }
        if (compatibilitySearch.value) params.keyword = compatibilitySearch.value
        if (compatibilityStatus.value !== 'all') params.status = compatibilityStatus.value
        const { data } = await api.getCompatibilityRuns(params)
        compatibilityRuns.value = data.items || []
        compatibilityTotal.value = data.total || 0
    } catch (err) {
        console.error('获取兼容性报告失败', err)
    } finally {
        compatibilityLoading.value = false
    }
}

const handleCompatibilitySearch = () => { compatibilityPage.value = 1; fetchCompatibilityRuns() }
const handleCompatibilityPageChange = (val) => { compatibilityPage.value = val; fetchCompatibilityRuns() }
const handleCompatibilitySizeChange = (val) => { compatibilityPageSize.value = val; compatibilityPage.value = 1; fetchCompatibilityRuns() }

const fetchInspectionRuns = async () => {
    if (!userStore.featureFlags?.model_inspection) return
    inspectionLoading.value = true
    try {
        const params = { page: inspectionPage.value, page_size: inspectionPageSize.value }
        if (inspectionSearch.value) params.keyword = inspectionSearch.value
        if (inspectionStatus.value !== 'all') params.status = inspectionStatus.value
        const { data } = await api.getInspectionRuns(params)
        inspectionRuns.value = data.items || []
        inspectionTotal.value = data.total || 0
    } catch (err) {
        console.error('获取巡检报告失败', err)
        ElMessage.error(err.response?.data?.detail || '获取巡检报告失败')
    } finally {
        inspectionLoading.value = false
    }
}
const handleInspectionSearch = () => { inspectionPage.value = 1; fetchInspectionRuns() }
const handleInspectionPageChange = val => { inspectionPage.value = val; fetchInspectionRuns() }
const handleInspectionSizeChange = val => { inspectionPageSize.value = val; inspectionPage.value = 1; fetchInspectionRuns() }
const handleInspectionView = id => { router.push(`/execution/reports/inspection/${id}`) }
const isInspectionRunActive = run => (
    ['PENDING', 'RUNNING', 'QUEUED'].includes(String(run?.status || '').toUpperCase())
    || run?.current_stage === '取消中'
)
const hasRunningInspectionRuns = computed(() => inspectionRuns.value.some(item => (
    isInspectionRunActive(item)
)))

const handleInspectionDelete = async (row) => {
    if (isInspectionRunActive(row)) {
        ElMessage.warning('运行中的巡检任务不能删除，请先取消任务')
        return
    }
    try {
        await ElMessageBox.confirm(
            `确定删除智能巡检报告「${row.name}」？相关截图、XML、日志和状态图数据也将被删除。`,
            '删除确认',
            { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
        )
        inspectionDeletingId.value = row.id
        await api.deleteInspectionRun(row.id)
        ElMessage.success('智能巡检报告已删除')
        if (inspectionPage.value > 1 && inspectionRuns.value.length === 1) {
            inspectionPage.value -= 1
        }
        await fetchInspectionRuns()
    } catch (err) {
        if (!['cancel', 'close'].includes(err)) {
            ElMessage.error(err.response?.data?.detail || '删除失败')
        }
    } finally {
        inspectionDeletingId.value = null
    }
}
const handleInspectionCommand = (command, row) => {
    if (command === 'delete' && !isInspectionRunActive(row)) handleInspectionDelete(row)
}

const resolveDeviceForRow = (item) => {
    const dev = devicesMap.value[item.device_serial]
    if (dev) {
        const p = dev.custom_name || dev.market_name || dev.model
        if (p) return p
    }
    return item.device_serial
}

const fbTasksWithDevice = computed(() => (
    fbTasks.value.map(item => ({ ...item, _resolved_device: resolveDeviceForRow(item) }))
))

const startupTasksWithDevice = computed(() => (
    startupTasks.value.map(item => ({ ...item, _resolved_device: resolveDeviceForRow(item) }))
))

const handleFbView = (taskId) => { router.push(`/special/fastbot/report/${taskId}`) }
const handleCompatibilityView = (runId) => { router.push(`/execution/reports/compatibility/${runId}`) }
const isCompatibilityRunning = (status) => ['PENDING', 'RUNNING'].includes(String(status || '').toUpperCase())

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

const handleCompatibilityDelete = async (row) => {
    try {
        await ElMessageBox.confirm(
            `确定删除兼容性报告「${row.name}」？相关截图、XML 和差异图文件也将被删除。`,
            '警告',
            { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
        )
        await api.deleteCompatibilityRun(row.id)
        ElMessage.success('已删除')
        await fetchCompatibilityRuns()
    } catch (err) {
        if (!['cancel', 'close'].includes(err)) {
            ElMessage.error(err.response?.data?.detail || '删除失败')
        }
    }
}

const getCompatibilityStatusText = (status) => String(status || 'PENDING').toUpperCase()
const isInstalledReplay = run => compatibilityExecutionMode(run) === 'installed_replay'
const getCompatibilityMode = run => {
    if (isInstalledReplay(run)) return '升级后回放'
    return run?.mode === 'clean' ? '干净对比' : '升级兼容'
}
const getCompatibilitySource = run => {
    if (isInstalledReplay(run)) {
        return run.inspection_run_id ? `巡检 #${run.inspection_run_id} · ${run.replay_branch_key || '-'}` : '巡检来源已清理'
    }
    return run.page_set?.name || '-'
}
const getCompatibilityPackage = run => {
    if (isInstalledReplay(run)) return packageSnapshotLabel(run.target_package_snapshot || {})
    return run.package_name || '-'
}
const getCompatibilityPageCount = run => (
    run.page_set?.pages?.length || run.total_chains || run.total_pages || 0
)
const hasRunningCompatibilityRuns = computed(() => (
    compatibilityRuns.value.some(item => isCompatibilityRunning(item.status))
))

const getFbDuration = (task) => {
    if (!task.started_at) return '-'
    const end = task.finished_at ? dayjs(task.finished_at) : dayjs()
    const secs = end.diff(dayjs(task.started_at), 'second')
    if (secs < 60) return `${secs}s`
    const m = Math.floor(secs / 60)
    const s = secs % 60
    return `${m}m ${s}s`
}

const formatMs = (value) => {
    const num = Number(value)
    return Number.isFinite(num) ? `${num} ms` : '-'
}

const getStartupAggregateValue = (task, mode, key) => task.summary?.startup_aggregate?.[mode]?.[key]
const getStartupSlowCount = (task) => Number(task.summary?.slow_count || 0)
const getStartupModes = (task) => {
    const modes = task.summary?.startup_config?.startup_modes || []
    if (!Array.isArray(modes) || modes.length === 0) return '-'
    return modes.map(mode => mode === 'cold' ? '冷启动' : '热启动').join(' / ')
}
const getStartupIterations = (task) => task.summary?.startup_config?.iterations || task.duration || '-'

const handleTabChange = (tab) => {
    fetchDevices()
    if (tab === 'fastbot') {
        fetchFbTasks()
        return
    }
    if (tab === 'startup') {
        fetchStartupTasks()
        return
    }
    if (tab === 'compatibility') {
        fetchCompatibilityRuns()
        return
    }
    if (tab === 'inspection') {
        fetchInspectionRuns()
        return
    }
    fetchData()
}

const handleReportTabChange = tab => {
    const next = resolveAvailableTab(tab)
    if (activeTab.value !== next) activeTab.value = next
    syncReportTabQuery(next)
    handleTabChange(next)
}

const startFbPolling = () => {
    if (fbPollTimer) return
    fbPollTimer = setInterval(() => {
        if (activeTab.value === 'fastbot') fetchFbTasks()
        if (activeTab.value === 'startup') fetchStartupTasks()
        if (activeTab.value === 'compatibility' && hasRunningCompatibilityRuns.value) fetchCompatibilityRuns()
        if (activeTab.value === 'inspection' && hasRunningInspectionRuns.value) fetchInspectionRuns()
    }, 15000)
}

const stopFbPolling = () => {
    if (!fbPollTimer) return
    clearInterval(fbPollTimer)
    fbPollTimer = null
}

const activatePage = () => {
    if (pageActive) return
    pageActive = true
    startFbPolling()
}

const deactivatePage = () => {
    if (!pageActive) return
    pageActive = false
    stopFbPolling()
}

const refreshReportCenter = () => {
    fetchDevices()
    fetchData()
    fetchFbTasks()
    fetchStartupTasks()
    fetchCompatibilityRuns()
    fetchInspectionRuns()
}

watch(
    () => route.query.tab,
    (tab) => {
        const next = resolveAvailableTab(tab)
        activeTab.value = next
        if (String(tab || '') === 'inspection' && next !== 'inspection') {
            syncReportTabQuery(next)
        }
        if (pageActive && activeTab.value === 'fastbot' && fbTasks.value.length === 0) {
            fetchFbTasks()
        }
        if (pageActive && activeTab.value === 'startup' && startupTasks.value.length === 0) {
            fetchStartupTasks()
        }
        if (pageActive && activeTab.value === 'compatibility' && compatibilityRuns.value.length === 0) {
            fetchCompatibilityRuns()
        }
        if (pageActive && activeTab.value === 'inspection' && inspectionRuns.value.length === 0) {
            fetchInspectionRuns()
        }
    }
)

watch(inspectionEnabled, enabled => {
    if (enabled) {
        if (route.query.tab === 'inspection' && activeTab.value !== 'inspection') {
            activeTab.value = 'inspection'
            if (pageActive) handleTabChange('inspection')
        }
        return
    }
    if (activeTab.value !== 'inspection') {
        if (route.query.tab === 'inspection') syncReportTabQuery('ui')
        return
    }
    activeTab.value = 'ui'
    syncReportTabQuery('ui')
    if (pageActive) handleTabChange('ui')
})

onMounted(() => {
    refreshReportCenter()
    activatePage()
})

onActivated(() => {
    refreshReportCenter()
    activatePage()
})

onDeactivated(() => {
    deactivatePage()
})

onUnmounted(() => {
    deactivatePage()
})
</script>

<template>
    <div v-if="isMobileMode" class="mobile-report-page">
        <el-segmented
            v-if="mobileReportOptions.length > 1"
            v-model="mobileReportTab"
            :options="mobileReportOptions"
            class="mobile-report-type"
        />
        <div class="mobile-report-toolbar">
            <el-input
                v-if="mobileReportTab === 'inspection'"
                v-model="inspectionSearch"
                placeholder="搜索巡检任务..."
                :prefix-icon="Search"
                clearable
                class="mobile-search-input"
                @keyup.enter="handleInspectionSearch"
                @clear="handleInspectionSearch"
            />
            <el-input
                v-else
                v-model="searchQuery"
                placeholder="搜索场景..."
                :prefix-icon="Search"
                clearable
                class="mobile-search-input"
                @keyup.enter="handleSearch"
                @clear="handleSearch"
            />
            <el-button :icon="Refresh" circle @click="mobileReportTab === 'inspection' ? fetchInspectionRuns() : fetchData()" />
        </div>

        <el-radio-group v-if="mobileReportTab === 'inspection'" v-model="inspectionStatus" class="mobile-status-filter" @change="handleInspectionSearch">
            <el-radio-button value="all">全部</el-radio-button>
            <el-radio-button value="pass">已完成</el-radio-button>
            <el-radio-button value="warning">需关注</el-radio-button>
            <el-radio-button value="fail">失败</el-radio-button>
            <el-radio-button value="running">运行中</el-radio-button>
        </el-radio-group>
        <el-radio-group v-else v-model="filterStatus" class="mobile-status-filter" @change="handleSearch">
            <el-radio-button value="all">全部</el-radio-button>
            <el-radio-button value="pass">成功</el-radio-button>
            <el-radio-button value="warning">告警</el-radio-button>
            <el-radio-button value="fail">失败</el-radio-button>
        </el-radio-group>

        <div v-if="mobileReportTab === 'inspection'" class="mobile-report-list" v-loading="inspectionLoading">
            <article
                v-for="row in inspectionRuns"
                :key="row.id"
                class="mobile-report-card mobile-inspection-card"
                @click="handleInspectionView(row.id)"
            >
                <header class="mobile-report-card-header">
                    <div class="mobile-report-title">
                        <strong>{{ row.name }}</strong>
                        <span>{{ formatDate(row.started_at || row.created_at) }}</span>
                    </div>
                    <el-tag :type="runStatusTagType(row.status)" size="small" effect="plain">
                        {{ inspectionRunStatusLabel(row) }}
                    </el-tag>
                </header>
                <div class="mobile-inspection-metrics">
                    <div>
                        <span>覆盖</span>
                        <strong>{{ inspectionRunCoverage(row).label }}</strong>
                        <small>{{ inspectionRunCoverage(row).detail }}</small>
                    </div>
                    <div>
                        <span>可回放</span>
                        <strong>{{ inspectionRunReplay(row).label }}</strong>
                        <small>{{ inspectionRunReplay(row).detail }}</small>
                    </div>
                </div>
                <div class="mobile-inspection-actions">
                    <el-button type="primary" link @click.stop="handleInspectionView(row.id)">查看报告</el-button>
                    <el-button
                        type="danger"
                        link
                        :disabled="isInspectionRunActive(row)"
                        :loading="inspectionDeletingId === row.id"
                        @click.stop="handleInspectionDelete(row)"
                    >删除</el-button>
                </div>
            </article>
            <el-empty v-if="!inspectionLoading && inspectionRuns.length === 0" description="暂无巡检报告" :image-size="90" />
        </div>

        <div v-else class="mobile-report-list" v-loading="loading">
            <article
                v-for="group in executions"
                :key="group.batch_id"
                class="mobile-report-card"
            >
                <header class="mobile-report-card-header">
                    <div class="mobile-report-title">
                        <strong>{{ group.batch_name }}</strong>
                        <span>{{ formatDate(group.start_time) }} · {{ group.executions.length }} 台设备</span>
                    </div>
                    <el-tag :type="runStatusTagType(group.status)" size="small">
                        {{ group.status === 'RUNNING' ? '运行中' : group.status }}
                    </el-tag>
                </header>

                <div class="mobile-report-executions">
                    <div
                        v-for="item in group.executions"
                        :key="item.id"
                        class="mobile-report-execution"
                    >
                        <div class="mobile-report-execution-main" @click="handleView(item.id)">
                            <strong>{{ formatDeviceName(item.device_serial, item.device_info) }}</strong>
                            <span>{{ getDuration(item.duration) }} · {{ item.executor_name || group.executor_name || '-' }}</span>
                        </div>
                        <div class="mobile-report-execution-actions">
                            <el-tag size="small" :type="runStatusTagType(item.status)">
                                {{ item.status }}
                            </el-tag>
                            <el-button
                                v-if="isExecutionRunning(item.status)"
                                type="danger"
                                link
                                :icon="CircleClose"
                                :loading="stoppingDeviceSerial === item.device_serial"
                                @click="handleStopExecutionFromReport(item)"
                            >
                                停止
                            </el-button>
                            <el-button v-else type="primary" link @click="handleView(item.id)">查看</el-button>
                        </div>
                    </div>
                </div>
            </article>
            <el-empty v-if="!loading && executions.length === 0" description="暂无测试记录" :image-size="90" />
        </div>

        <div class="mobile-pagination" v-if="mobileReportTab === 'inspection' && inspectionTotal > 0">
            <el-pagination
                v-model:current-page="inspectionPage"
                :page-size="inspectionPageSize"
                :background="true"
                layout="prev, pager, next"
                :total="inspectionTotal"
                @current-change="handleInspectionPageChange"
            />
        </div>
        <div class="mobile-pagination" v-else-if="mobileReportTab === 'ui' && totalRecords > 0">
            <el-pagination
                v-model:current-page="currentPage"
                :page-size="pageSize"
                :background="true"
                layout="prev, pager, next"
                :total="totalRecords"
                @current-change="handleCurrentChange"
            />
        </div>
    </div>

    <div v-else class="report-container">
        <div class="content-wrapper">
            <!-- 顶层 Tab 切换 -->
            <el-tabs v-model="activeTab" class="report-tabs" @tab-change="handleReportTabChange">
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
                                <el-radio-button value="all">全部</el-radio-button>
                                <el-radio-button value="pass">成功</el-radio-button>
                                <el-radio-button value="warning">告警</el-radio-button>
                                <el-radio-button value="fail">失败</el-radio-button>
                            </el-radio-group>
                        </div>
                        <div class="right-actions">
                            <el-button type="primary" plain :icon="TrendCharts" @click="showFlakyDrawer = true">
                                稳定性分析
                            </el-button>
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
                                                        📱 {{ formatDeviceName(subRow.device_serial, subRow.device_info) }}
                                                    </span>
                                                </template>
                                            </el-table-column>
                                            <el-table-column label="状态" width="100">
                                                <template #default="{ row: subRow }">
                                                    <el-tag :type="runStatusTagType(subRow.status)" size="small" effect="plain">
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
                                                    <el-divider direction="vertical" />
                                                    <el-button link type="danger" @click="handleDeleteExecution(subRow.id)" size="small" :disabled="subRow.status === 'RUNNING'">删除</el-button>
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
                                    <el-tag :type="runStatusTagType(row.status)">
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

                            <el-table-column label="操作" width="80" align="center">
                                <template #default="{ row }">
                                    <el-button :icon="Delete" link type="danger" @click.stop="handleDeleteBatch(row)" :disabled="row.status === 'RUNNING'" />
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
                                placeholder="搜索包名或设备..."
                                :prefix-icon="Search"
                                clearable
                                class="search-input"
                                @keyup.enter="handleFbSearch"
                                @clear="handleFbSearch"
                            />
                        </div>
                        <div class="right-actions">
                            <el-button :icon="Refresh" circle @click="fetchFbTasks" />
                        </div>
                    </div>

                    <!-- 探索报告表格 -->
                    <div class="list-scroll-area" v-loading="fbLoading">
                    <el-table
                        :data="fbTasksWithDevice"
                        style="width: 100%"
                        :header-cell-style="{ background: '#f5f7fa', color: '#606266' }"
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
                                <el-tag :type="runStatusTagType(row.status)" size="small" effect="plain">{{ row.status }}</el-tag>
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
                    </div>

                    <div class="pagination-footer" v-if="fbTotal > 0">
                        <el-pagination
                            v-model:current-page="fbPage"
                            v-model:page-size="fbPageSize"
                            :page-sizes="[10, 20, 50, 100]"
                            :background="true"
                            layout="total, sizes, prev, pager, next, jumper"
                            :total="fbTotal"
                            @size-change="handleFbSizeChange"
                            @current-change="handleFbPageChange"
                        />
                    </div>
                </el-tab-pane>

                <el-tab-pane label="冷热启动报告" name="startup">
                    <div class="list-header">
                        <div class="left-filters">
                            <el-input
                                v-model="startupSearch"
                                placeholder="搜索包名或设备..."
                                :prefix-icon="Search"
                                clearable
                                class="search-input"
                                @keyup.enter="handleStartupSearch"
                                @clear="handleStartupSearch"
                            />
                        </div>
                        <div class="right-actions">
                            <el-button :icon="Refresh" circle @click="fetchStartupTasks" />
                        </div>
                    </div>

                    <div class="list-scroll-area" v-loading="startupLoading">
                    <el-table
                        :data="startupTasksWithDevice"
                        style="width: 100%"
                        :header-cell-style="{ background: '#f5f7fa', color: '#606266' }"
                    >
                        <el-table-column prop="id" label="ID" width="60" align="center" />
                        <el-table-column label="目标包名" min-width="190">
                            <template #default="{ row }">
                                <span class="pkg-name">{{ row.package_name }}</span>
                            </template>
                        </el-table-column>
                        <el-table-column label="运行设备" width="170">
                            <template #default="{ row }">{{ row._resolved_device }}</template>
                        </el-table-column>
                        <el-table-column label="启动模式" width="130" align="center">
                            <template #default="{ row }">{{ getStartupModes(row) }}</template>
                        </el-table-column>
                        <el-table-column label="启动次数" width="90" align="center">
                            <template #default="{ row }">{{ getStartupIterations(row) }}</template>
                        </el-table-column>
                        <el-table-column label="冷启动就绪 P90" width="140" align="center">
                            <template #default="{ row }">{{ formatMs(getStartupAggregateValue(row, 'cold', 'ready_p90_ms')) }}</template>
                        </el-table-column>
                        <el-table-column label="热启动就绪 P90" width="140" align="center">
                            <template #default="{ row }">{{ formatMs(getStartupAggregateValue(row, 'hot', 'ready_p90_ms')) }}</template>
                        </el-table-column>
                        <el-table-column label="冷启动 Total P90" width="140" align="center">
                            <template #default="{ row }">{{ formatMs(getStartupAggregateValue(row, 'cold', 'p90_ms')) }}</template>
                        </el-table-column>
                        <el-table-column label="热启动 Total P90" width="140" align="center">
                            <template #default="{ row }">{{ formatMs(getStartupAggregateValue(row, 'hot', 'p90_ms')) }}</template>
                        </el-table-column>
                        <el-table-column label="慢启动" width="90" align="center">
                            <template #default="{ row }">
                                <span :class="{ 'text-danger': getStartupSlowCount(row) > 0 }">{{ getStartupSlowCount(row) }}</span>
                            </template>
                        </el-table-column>
                        <el-table-column label="开始时间" width="140" align="center">
                            <template #default="{ row }">{{ formatDate(row.started_at || row.created_at) }}</template>
                        </el-table-column>
                        <el-table-column label="执行人" width="120" align="center">
                            <template #default="{ row }">{{ row.executor_name || '-' }}</template>
                        </el-table-column>
                        <el-table-column label="状态" width="100" align="center">
                            <template #default="{ row }">
                                <el-tag :type="runStatusTagType(row.status)" size="small" effect="plain">{{ row.status }}</el-tag>
                            </template>
                        </el-table-column>
                        <el-table-column label="操作" width="120" align="center" fixed="right">
                            <template #default="{ row }">
                                <el-tooltip v-if="row.status === 'COMPLETED' || row.report_ready" content="查看报告" placement="top">
                                    <el-button :icon="View" link type="primary" @click="handleFbView(row.id)" />
                                </el-tooltip>
                                <el-tooltip content="删除" placement="top">
                                    <el-button :icon="Delete" link type="danger" @click="handleFbDelete(row)" />
                                </el-tooltip>
                            </template>
                        </el-table-column>
                    </el-table>
                    </div>

                    <div class="pagination-footer" v-if="startupTotal > 0">
                        <el-pagination
                            v-model:current-page="startupPage"
                            v-model:page-size="startupPageSize"
                            :page-sizes="[10, 20, 50, 100]"
                            :background="true"
                            layout="total, sizes, prev, pager, next, jumper"
                            :total="startupTotal"
                            @size-change="handleStartupSizeChange"
                            @current-change="handleStartupPageChange"
                        />
                    </div>
                </el-tab-pane>

                <el-tab-pane label="兼容性报告" name="compatibility">
                    <div class="list-header">
                        <div class="left-filters">
                            <el-input
                                v-model="compatibilitySearch"
                                placeholder="搜索任务或包名..."
                                :prefix-icon="Search"
                                clearable
                                class="search-input"
                                @keyup.enter="handleCompatibilitySearch"
                                @clear="handleCompatibilitySearch"
                            />
                            <el-radio-group v-model="compatibilityStatus" @change="handleCompatibilitySearch">
                                <el-radio-button value="all">全部</el-radio-button>
                                <el-radio-button value="pass">通过</el-radio-button>
                                <el-radio-button value="warning">警告</el-radio-button>
                                <el-radio-button value="fail">失败</el-radio-button>
                                <el-radio-button value="running">运行中</el-radio-button>
                            </el-radio-group>
                        </div>
                        <div class="right-actions">
                            <el-button :icon="Refresh" circle @click="fetchCompatibilityRuns" />
                        </div>
                    </div>

                    <div class="list-scroll-area" v-loading="compatibilityLoading">
                    <el-table
                        :data="compatibilityRuns"
                        style="width: 100%"
                        :header-cell-style="{ background: '#f5f7fa', color: '#606266' }"
                    >
                        <el-table-column prop="id" label="ID" width="64" align="center" />
                        <el-table-column label="任务" min-width="220">
                            <template #default="{ row }">
                                <div class="run-name">
                                    {{ row.name }}
                                    <el-tag v-if="row.compare_mode === 'device'" size="small" type="warning" effect="plain">机型对比</el-tag>
                                    <el-tag v-if="isInstalledReplay(row)" size="small" type="success" effect="plain">升级后回放</el-tag>
                                </div>
                                <div class="muted-text">{{ getCompatibilityPackage(row) }}</div>
                            </template>
                        </el-table-column>
                        <el-table-column label="来源" min-width="180">
                            <template #default="{ row }">{{ getCompatibilitySource(row) }}</template>
                        </el-table-column>
                        <el-table-column label="模式" width="100" align="center">
                            <template #default="{ row }">{{ getCompatibilityMode(row) }}</template>
                        </el-table-column>
                        <el-table-column label="设备/链路" width="100" align="center">
                            <template #default="{ row }">{{ row.total_cells }} / {{ getCompatibilityPageCount(row) }}</template>
                        </el-table-column>
                        <el-table-column label="结果" width="120" align="center">
                            <template #default="{ row }">
                                <span class="summary-count pass">{{ row.pass_count }}</span>
                                <span class="summary-count warn">{{ row.warning_count }}</span>
                                <span class="summary-count fail">{{ row.fail_count }}</span>
                            </template>
                        </el-table-column>
                        <el-table-column label="状态" width="110" align="center">
                            <template #default="{ row }">
                                <el-tag :type="runStatusTagType(row.status)" size="small" effect="plain">
                                    {{ getCompatibilityStatusText(row.status) }}
                                </el-tag>
                            </template>
                        </el-table-column>
                        <el-table-column label="开始时间" width="150" align="center">
                            <template #default="{ row }">{{ formatDate(row.started_at || row.created_at) }}</template>
                        </el-table-column>
                        <el-table-column label="执行人" width="120" align="center">
                            <template #default="{ row }">{{ row.executor_name || '-' }}</template>
                        </el-table-column>
                        <el-table-column label="操作" width="120" align="center" fixed="right">
                            <template #default="{ row }">
                                <el-tooltip content="查看报告" placement="top">
                                    <el-button :icon="View" link type="primary" @click="handleCompatibilityView(row.id)" />
                                </el-tooltip>
                                <el-tooltip content="删除" placement="top">
                                    <el-button
                                        :icon="Delete"
                                        link
                                        type="danger"
                                        :disabled="isCompatibilityRunning(row.status)"
                                        @click="handleCompatibilityDelete(row)"
                                    />
                                </el-tooltip>
                            </template>
                        </el-table-column>
                    </el-table>
                    </div>

                    <div class="pagination-footer" v-if="compatibilityTotal > 0">
                        <el-pagination
                            v-model:current-page="compatibilityPage"
                            v-model:page-size="compatibilityPageSize"
                            :page-sizes="[10, 20, 50, 100]"
                            :background="true"
                            layout="total, sizes, prev, pager, next, jumper"
                            :total="compatibilityTotal"
                            @size-change="handleCompatibilitySizeChange"
                            @current-change="handleCompatibilityPageChange"
                        />
                    </div>
                </el-tab-pane>

                <el-tab-pane
                    v-if="userStore.featureFlags?.model_inspection"
                    label="智能巡检报告"
                    name="inspection"
                >
                    <div class="list-header">
                        <div class="left-filters">
                            <el-input
                                v-model="inspectionSearch"
                                placeholder="搜索巡检任务..."
                                :prefix-icon="Search"
                                clearable
                                class="search-input"
                                @keyup.enter="handleInspectionSearch"
                                @clear="handleInspectionSearch"
                            />
                            <el-radio-group v-model="inspectionStatus" @change="handleInspectionSearch">
                                <el-radio-button value="all">全部</el-radio-button>
                                <el-radio-button value="pass">已完成</el-radio-button>
                                <el-radio-button value="warning">需关注</el-radio-button>
                                <el-radio-button value="fail">失败</el-radio-button>
                                <el-radio-button value="running">运行中</el-radio-button>
                            </el-radio-group>
                        </div>
                        <el-button :icon="Refresh" circle @click="fetchInspectionRuns" />
                    </div>

                    <div class="list-scroll-area" v-loading="inspectionLoading">
                        <el-table
                            :data="inspectionRuns"
                            class="inspection-report-table"
                            style="width: 100%"
                            :header-cell-style="{ background: '#f5f7fa', color: '#606266' }"
                            row-class-name="inspection-report-row"
                            @row-click="row => handleInspectionView(row.id)"
                        >
                            <el-table-column label="任务" min-width="290">
                                <template #default="{ row }">
                                    <div class="inspection-task-cell">
                                        <el-tooltip :content="row.name" placement="top-start">
                                            <div class="run-name inspection-run-name">{{ row.name }}</div>
                                        </el-tooltip>
                                        <div class="muted-text inspection-run-package">{{ row.package_name || '-' }}</div>
                                    </div>
                                </template>
                            </el-table-column>
                            <el-table-column label="覆盖" width="180">
                                <template #default="{ row }">
                                    <div class="inspection-run-metric">
                                        <strong>{{ inspectionRunCoverage(row).label }}</strong>
                                        <el-tooltip :content="inspectionRunCoverage(row).detail" placement="top">
                                            <span>{{ inspectionRunCoverage(row).detail }}</span>
                                        </el-tooltip>
                                    </div>
                                </template>
                            </el-table-column>
                            <el-table-column label="可回放" width="190">
                                <template #default="{ row }">
                                    <div class="inspection-run-metric">
                                        <strong>{{ inspectionRunReplay(row).label }}</strong>
                                        <el-tooltip :content="inspectionRunReplay(row).detail" placement="top">
                                            <span>{{ inspectionRunReplay(row).detail }}</span>
                                        </el-tooltip>
                                    </div>
                                </template>
                            </el-table-column>
                            <el-table-column label="开始时间" width="150" align="center">
                                <template #default="{ row }">{{ formatDate(row.started_at || row.created_at) }}</template>
                            </el-table-column>
                            <el-table-column label="状态" width="124" align="center">
                                <template #default="{ row }">
                                    <el-tag :type="runStatusTagType(row.status)" size="small" effect="plain">
                                        {{ inspectionRunStatusLabel(row) }}
                                    </el-tag>
                                </template>
                            </el-table-column>
                            <el-table-column label="操作" width="88" fixed="right" align="center">
                                <template #default="{ row }">
                                    <el-tooltip content="查看报告" placement="top">
                                        <el-button :icon="View" link type="primary" aria-label="查看报告" @click.stop="handleInspectionView(row.id)" />
                                    </el-tooltip>
                                    <el-dropdown
                                        trigger="click"
                                        @click.stop
                                        @command="command => handleInspectionCommand(command, row)"
                                    >
                                        <el-button :icon="MoreFilled" link aria-label="更多操作" @click.stop />
                                        <template #dropdown>
                                            <el-dropdown-menu>
                                                <el-dropdown-item command="delete" :disabled="isInspectionRunActive(row)">删除报告</el-dropdown-item>
                                            </el-dropdown-menu>
                                        </template>
                                    </el-dropdown>
                                </template>
                            </el-table-column>
                        </el-table>
                    </div>

                    <div class="pagination-footer" v-if="inspectionTotal > 0">
                        <el-pagination
                            v-model:current-page="inspectionPage"
                            v-model:page-size="inspectionPageSize"
                            :page-sizes="[10, 20, 50, 100]"
                            :background="true"
                            layout="total, sizes, prev, pager, next, jumper"
                            :total="inspectionTotal"
                            @size-change="handleInspectionSizeChange"
                            @current-change="handleInspectionPageChange"
                        />
                    </div>
                </el-tab-pane>
            </el-tabs>
        </div>

        <FlakyAnalysisDrawer v-model="showFlakyDrawer" />
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

.run-name {
    font-weight: 600;
    color: #303133;
}

.muted-text {
    color: #909399;
    font-size: 12px;
}

.summary-count {
    margin: 0 4px;
    font-weight: 700;
}

.inspection-run-metric {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
    line-height: 1.3;
}

.inspection-run-metric strong {
    min-width: 0;
    color: #303133;
    font-size: 13px;
}

.inspection-run-metric span {
    display: block;
    min-width: 0;
    overflow: hidden;
    color: #909399;
    font-size: 11px;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.inspection-task-cell {
    min-width: 0;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 3px;
}

.inspection-task-cell > .el-tooltip,
.inspection-task-cell > .inspection-run-package { max-width: 100%; min-width: 0; }
.inspection-run-name { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.inspection-run-package { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.inspection-report-table :deep(.el-table__cell) { min-width: 0; }
.inspection-report-table :deep(.el-table__fixed-right) { box-shadow: -4px 0 8px rgba(31, 35, 41, 0.06); }
.list-scroll-area :deep(.inspection-report-row) { cursor: pointer; }

@media (max-width: 1100px) {
    .content-wrapper { padding: 14px; }
    .list-header { align-items: stretch; gap: 10px; flex-wrap: wrap; }
    .left-filters { min-width: 0; flex: 1 1 100%; gap: 10px; flex-wrap: wrap; }
    .search-input { flex: 1 1 220px; width: auto; }
    .pagination-footer { max-width: 100%; overflow-x: auto; padding-bottom: 2px; }
}

.pass { color: #67C23A; }
.warn { color: #E6A23C; }
.fail { color: #F56C6C; }

.text-danger { color: #F56C6C; font-weight: 600; }
.text-warning { color: #E6A23C; font-weight: 600; }

.mobile-report-page {
    height: 100%;
    display: flex;
    flex-direction: column;
    background: #f6f7f9;
    padding: 12px;
    box-sizing: border-box;
    overflow: hidden;
}

.mobile-report-type {
    width: 100%;
    margin-bottom: 10px;
    flex-shrink: 0;
}

.mobile-report-toolbar {
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
    flex-shrink: 0;
    overflow-x: auto;
    max-width: 100%;
    padding-bottom: 2px;
    white-space: nowrap;
}

.mobile-report-list {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.mobile-report-card {
    border: 1px solid #ebeef5;
    border-radius: 8px;
    background: #ffffff;
    padding: 14px;
}

.mobile-inspection-card { cursor: pointer; }
.mobile-inspection-metrics { margin-top: 12px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); border-top: 1px solid #ebeef5; border-bottom: 1px solid #ebeef5; }
.mobile-inspection-metrics > div { min-width: 0; padding: 10px 8px; display: flex; flex-direction: column; gap: 3px; }
.mobile-inspection-metrics > div + div { border-left: 1px solid #ebeef5; }
.mobile-inspection-metrics span, .mobile-inspection-metrics small { min-width: 0; overflow-wrap: anywhere; color: #909399; font-size: 11px; line-height: 1.4; }
.mobile-inspection-metrics strong { color: #303133; font-size: 15px; }
.mobile-inspection-actions { display: flex; align-items: center; justify-content: flex-end; gap: 6px; padding-top: 6px; }

.mobile-report-card-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
}

.mobile-report-title {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
}

.mobile-report-title strong {
    font-size: 15px;
    color: #303133;
    overflow: hidden;
    text-overflow: ellipsis;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-height: 1.35;
}

.mobile-report-title span {
    font-size: 12px;
    color: #909399;
}

.mobile-report-executions {
    margin-top: 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.mobile-report-execution {
    border-radius: 6px;
    background: #f6f7f9;
    padding: 10px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
}

.mobile-report-execution-main {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
    cursor: pointer;
}

.mobile-report-execution-main strong {
    font-size: 13px;
    color: #303133;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.mobile-report-execution-main span {
    font-size: 12px;
    color: #909399;
}

.mobile-report-execution-actions {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
}

.mobile-pagination {
    padding-top: 10px;
    display: flex;
    justify-content: center;
    flex-shrink: 0;
}

@media (max-width: 460px) {
    .mobile-inspection-metrics { grid-template-columns: minmax(0, 1fr); }
    .mobile-inspection-metrics > div + div { border-top: 1px solid #ebeef5; border-left: 0; }
}
</style>
