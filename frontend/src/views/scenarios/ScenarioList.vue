<script setup>
import { ref, onActivated, onDeactivated, onUnmounted, computed, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { Plus, Search, VideoPlay, Edit, Delete, Refresh, MoreFilled,
         Check, Close, Timer, CircleClose, FolderOpened } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import FolderTreePanel from '@/components/FolderTreePanel.vue'
import { deviceStatusLabel, deviceStatusTagType, runStatusColor } from '@/utils/statusMeta'
import { useUserStore } from '@/stores/useUserStore'
import dayjs from 'dayjs'
import { useClientMode } from '@/composables/useClientMode'
import { createPollingLoop } from '@/utils/polling'

const router = useRouter()
const userStore = useUserStore()
const { isMobileMode } = useClientMode()

// ==================== Folder Tree ====================
const selectedFolderId = ref(null)
const treePanelRef = ref(null)

const refreshFolderTree = () => treePanelRef.value?.refresh()

const fetchScenarioFolderTree = async () => {
    const res = await api.getScenarioFolderTree()
    return { tree: res.data.tree || [], items: res.data.all_scenarios || [] }
}

const handleFolderSelect = (folderId) => {
    selectedFolderId.value = folderId
    currentPage.value = 1
    fetchScenarios()
}

const handleOpenScenario = (node) => {
    router.push(`/ui/scenarios/${node.scenario_id}/edit`)
}

// Data
const scenarios = ref([])
const loading = ref(false)
const loadError = ref('')
const searchQuery = ref('')
const filterStatus = ref('all') // all, success, warning, failure
const currentUser = computed(() => userStore.userInfo || {})
const isAdmin = computed(() => currentUser.value.role === 'admin')
const activeScenarioRuns = ref({})
const ACTIVE_RUN_POLL_INTERVAL_MS = 3000

// Pagination
const currentPage = ref(1)
const pageSize = ref(20)
const total = ref(0)


// Methods
const fetchScenarios = async () => {
    loading.value = true
    loadError.value = ''
    try {
        const params = {
            skip: (currentPage.value - 1) * pageSize.value,
            limit: pageSize.value,
            keyword: searchQuery.value || undefined
        }
        if (selectedFolderId.value !== null) {
            params.folder_id = selectedFolderId.value
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
        loadError.value = '场景列表加载失败，请重试'
        ElMessage.error(loadError.value)
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
    const query = {}
    if (selectedFolderId.value !== null) {
        query.folder_id = selectedFolderId.value
    }
    router.push({ path: '/ui/scenarios/create', query })
}

const handleEdit = (id) => {
    router.push(`/ui/scenarios/${id}/edit`)
}

// ==================== Run Configuration ====================
const runDialogVisible = ref(false)
const runPhase = ref('')
const runBusy = computed(() => Boolean(runPhase.value))
const terminatingIds = ref(new Set())
const foldersVisible = ref(true)
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

const precheckScenarioOnDevice = async (scenarioId, serial, selectedEnvironment = runForm.envId) => {
    try {
        const { data } = await api.precheckScenario(scenarioId, selectedEnvironment, serial)
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

const scenarioQueueInfo = (row) => activeScenarioRuns.value[row.id]?.queue || null

const scenarioQueueSuffix = (row) => {
    const queue = scenarioQueueInfo(row)
    return queue?.position ? `（第 ${queue.position} 位）` : ''
}

const statusLabel = (item) => ({ pass: '已通过', success: '已通过', warning: '有告警', fail: '失败', failed: '失败', running: '执行中', queued: `排队中${scenarioQueueSuffix(item)}`, aborted: '已终止', cancelled: '已终止' }[normalizeRunStatus(item.last_run_status)] || '未执行')

const scenarioStatusText = (item) => {
    const s = normalizeRunStatus(item.last_run_status)
    if (s === 'queued') return `排队中${scenarioQueueSuffix(item)}`
    return s || 'not_run'
}

// 从 /runs/active 的 items 构建活跃 run 条目（含排队信息）
const buildActiveScenarioEntry = (items, fallback = {}) => {
    const entry = {
        batch_id: items[0]?.batch_id ?? fallback.batch_id,
        execution_ids: items.map(item => item.execution_id).filter(Boolean),
        device_serials: items.map(item => item.device_serial).filter(Boolean)
    }
    const queuedItems = items.filter(item => String(item.status || '').toUpperCase() === 'QUEUED')
    if (queuedItems.length > 0) {
        const positions = queuedItems.map(item => item.queue_position).filter(p => Number.isFinite(p))
        entry.queue = {
            count: queuedItems.length,
            position: positions.length ? Math.min(...positions) : null
        }
    }
    return entry
}

const handleRunClick = async (row) => {
    if (runBusy.value || terminatingIds.value.has(row.id)) return
    if (isScenarioRunActive(row)) {
        await terminateScenarioRun(row)
        return
    }
    runningScenarioId.value = row.id
    runDialogVisible.value = true
    fetchRunConfigOptions()
}

const confirmRun = async () => {
    if (runBusy.value) return
    if (!runningScenarioId.value) return
    if (!runForm.deviceSerials || runForm.deviceSerials.length === 0) {
        ElMessage.warning('请至少选择一台设备')
        return
    }
    const targetId = runningScenarioId.value
    const selectedEnvironment = runForm.envId
    const selectedDevices = [...runForm.deviceSerials]
    runPhase.value = 'prechecking'
    try {
        const runnable = []
        const blocked = []
        for (const serial of selectedDevices) {
            const check = await precheckScenarioOnDevice(targetId, serial, selectedEnvironment)
            if (check.ok) runnable.push(serial)
            else blocked.push({ device_serial: serial, reason: check.reason })
        }

        if (runnable.length === 0) {
            const first = blocked[0]
            ElMessage.error(`运行前预检未通过：${first ? `${first.device_serial} - ${first.reason}` : '无可执行设备'}`)
            return
        }

        runPhase.value = 'submitting'
        const { data } = await api.runScenario(targetId, selectedEnvironment, runnable)
        const backendBlocked = Array.isArray(data?.blocked_prechecks) ? data.blocked_prechecks : []
        const allBlocked = blocked.concat(backendBlocked)

        // 并发超限的任务会进入 FIFO 队列而非失败：区分"已开始 / 已加入队列"
        const runs = Array.isArray(data?.runs) ? data.runs : []
        const queuedRuns = runs.filter(item => item.queued)
        const startedCount = runs.length - queuedRuns.length
        const queuePositions = queuedRuns.map(item => item.queue_position).filter(p => Number.isFinite(p))
        const minQueuePosition = queuePositions.length ? Math.min(...queuePositions) : null

        let startMsg
        if (queuedRuns.length === 0) {
            startMsg = `场景已在 ${runnable.length} 台设备开始批次执行`
        } else if (startedCount === 0) {
            startMsg = `并发已满，场景已加入执行队列（${queuedRuns.length} 台${minQueuePosition ? `，最前第 ${minQueuePosition} 位` : ''}）`
        } else {
            startMsg = `已开始 ${startedCount} 台；${queuedRuns.length} 台已加入队列${minQueuePosition ? `（最前第 ${minQueuePosition} 位）` : ''}`
        }

        if (allBlocked.length > 0) {
            const first = allBlocked[0]
            ElMessage.warning(`${startMsg}；${allBlocked.length} 台预检失败（示例：${first.device_serial} - ${first.reason}）`)
        } else if (queuedRuns.length > 0) {
            ElMessage.info(startMsg)
        } else {
            ElMessage.success(startMsg)
        }

        runDialogVisible.value = false
        // Optimistic update
        const item = scenarios.value.find(s => s.id === targetId)
        if (item) item.last_run_status = startedCount > 0 || queuedRuns.length === 0 ? 'RUNNING' : 'QUEUED'
        const activeEntry = {
            batch_id: data?.batch_id,
            execution_ids: data?.execution_ids || [],
            device_serials: runnable
        }
        if (queuedRuns.length > 0) {
            activeEntry.queue = { count: queuedRuns.length, position: minQueuePosition }
        }
        activeScenarioRuns.value = {
            ...activeScenarioRuns.value,
            [targetId]: activeEntry
        }
        startActiveRunPolling()
        fetchScenarios()
    } catch (err) {
        ElMessage.error('启动失败: ' + summarizeHttpDetail(err))
    } finally {
        runPhase.value = ''
    }
}

const terminateScenarioRun = async (row) => {
    const active = activeScenarioRuns.value[row.id]
    if (!active || terminatingIds.value.has(row.id)) return
    terminatingIds.value.add(row.id)
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
    } finally {
        terminatingIds.value.delete(row.id)
    }
}

const restoreActiveScenarioRuns = async () => {
    const runningRows = scenarios.value.filter(row => ['running', 'queued'].includes(normalizeRunStatus(row.last_run_status)))
    if (runningRows.length === 0) return
    const next = { ...activeScenarioRuns.value }
    for (const row of runningRows) {
        try {
            const { data } = await api.getActiveRuns('scenario', row.id)
            const items = data?.items || []
            if (items.length > 0) {
                next[row.id] = buildActiveScenarioEntry(items, next[row.id])
            }
        } catch {}
    }
    activeScenarioRuns.value = next
    if (Object.keys(next).length > 0) startActiveRunPolling()
}

const pollActiveScenarioRuns = async () => {
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
            const items = data?.items || []
            if (items.length === 0) {
                delete next[scenarioId]
                changed = true
                continue
            }
            next[scenarioId] = buildActiveScenarioEntry(items, next[scenarioId])
            // 排队任务获得槽位后（QUEUED -> RUNNING）同步本地行状态
            const item = scenarios.value.find(s => s.id === Number(scenarioId))
            if (item && normalizeRunStatus(item.last_run_status) === 'queued' && !next[scenarioId].queue) {
                item.last_run_status = 'RUNNING'
            }
        } catch {}
    }
    activeScenarioRuns.value = next
    if (changed) fetchScenarios()
}

// 运行状态轮询：在途不重叠 + 页面隐藏即暂停（utils/polling.js 统一门闩）
const activeRunPollLoop = createPollingLoop({
    intervalMs: ACTIVE_RUN_POLL_INTERVAL_MS,
    tick: pollActiveScenarioRuns
})

const startActiveRunPolling = () => {
    stopActiveRunPolling()
    activeRunPollLoop.start()
}

const stopActiveRunPolling = () => {
    activeRunPollLoop.stop()
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
        refreshFolderTree()
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
const fullTime = value => value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '—'

const formatDate = (date) => {
    if (!date) return '-'
    const d = dayjs(date)
    if (d.isSame(dayjs(), 'day')) {
        return '今天 ' + d.format('HH:mm')
    }
    return d.format('MM-DD HH:mm')
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
    refreshFolderTree()
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

        <el-alert v-if="loadError" :title="loadError" type="error" show-icon :closable="false"><el-button link @click="fetchScenarios">重试</el-button></el-alert>
        <div class="mobile-scenario-list" v-loading="loading">
            <article
                v-for="item in scenarios"
                :key="item.id"
                class="mobile-scenario-card"
            >
                <div class="mobile-scenario-strip" :style="{ backgroundColor: runStatusColor(item.last_run_status) }"></div>
                <div class="mobile-scenario-body">
                    <div class="mobile-scenario-header">
                        <div class="mobile-scenario-title">
                            <strong>{{ item.name }}</strong>
                        </div>
                        <el-tag size="small" effect="plain">
                            {{ scenarioStatusText(item) }}
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
                        <el-button :type="isScenarioRunActive(item) ? 'danger' : 'primary'" :icon="isScenarioRunActive(item) ? CircleClose : VideoPlay" :disabled="runBusy" :loading="terminatingIds.has(item.id)" @click="handleRunClick(item)">{{ isScenarioRunActive(item) ? '终止' : '运行场景' }}</el-button>
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
                layout="prev, slot, next"
                :total="total"
                @current-change="handleCurrentChange"
            >
                <span class="mobile-page-position">{{ currentPage }} / {{ Math.ceil(total / pageSize) }}</span>
            </el-pagination>
        </div>
    </div>

    <div v-else class="scenario-list-container">
        <el-container class="main-layout">
            <!-- Left: Folder Tree（宽度由 FolderTreePanel 外壳拖拽调整，localStorage 持久化） -->
            <FolderTreePanel
                v-show="foldersVisible"
                ref="treePanelRef"
                class="folder-aside"
                title="场景目录"
                all-label="所有场景"
                item-id-key="scenario_id"
                :fetch-tree="fetchScenarioFolderTree"
                :create-folder="api.createScenarioFolder"
                :rename-folder="api.renameScenarioFolder"
                :delete-folder="api.deleteScenarioFolder"
                :move-item="api.moveScenario"
                @select-folder="handleFolderSelect"
                @open-item="handleOpenScenario"
                @item-moved="fetchScenarios"
            />

            <el-main class="list-main">
                <div class="content-wrapper">
            <!-- Header -->
            <div class="list-header">
                <div class="left-filters">
                            <el-button :icon="FolderOpened" :aria-expanded="foldersVisible" :title="foldersVisible ? '收起目录' : '展开目录'" @click="foldersVisible = !foldersVisible" />
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

            <el-alert v-if="loadError" :title="loadError" type="error" show-icon :closable="false"><el-button link @click="fetchScenarios">重试</el-button></el-alert>
            <!-- Scrollable List -->
            <div class="list-scroll-area" v-loading="loading">
                <el-table :data="scenarios" height="100%" class="ad-table">
                    <el-table-column label="场景名称" min-width="180"><template #default="{ row }"><button class="ad-name-button" :title="row.name" @click="handleEdit(row.id)">{{ row.name }}</button></template></el-table-column>
                    <el-table-column label="步骤" width="66" align="center"><template #default="{ row }">{{ row.step_count || 0 }}</template></el-table-column>
                    <el-table-column label="最近状态" width="142"><template #default="{ row }"><span :style="{ color: runStatusColor(row.last_run_status) }">{{ statusLabel(row) }}</span></template></el-table-column>
                    <el-table-column label="耗时" width="78"><template #default="{ row }">{{ getDuration(row) }}</template></el-table-column>
                    <el-table-column label="最后更新" width="136"><template #default="{ row }"><el-popover trigger="click" width="300"><template #reference><el-button text class="metadata-trigger" :aria-label="row.name + ' 的详细信息'">{{ formatDate(row.updated_at) }}</el-button></template><dl class="ad-metadata"><dt>ID</dt><dd>#{{ row.id }}</dd><dt>创建人</dt><dd>{{ row.creator_name || '—' }}</dd><dt>创建时间</dt><dd>{{ fullTime(row.created_at) }}</dd><dt>更新人</dt><dd>{{ row.updater_name || '—' }}</dd><dt>更新时间</dt><dd>{{ fullTime(row.updated_at) }}</dd><dt>最近执行人</dt><dd>{{ row.last_executor || '—' }}</dd><dt>执行时间</dt><dd>{{ fullTime(row.last_run_time) }}</dd><dt>失败位置</dt><dd>{{ row.last_failed_step || '—' }}</dd></dl></el-popover></template></el-table-column>
                    <el-table-column label="操作" width="128" fixed="right" align="right"><template #default="{ row }"><div class="ad-row-actions"><el-button link :type="isScenarioRunActive(row) ? 'danger' : 'primary'" :disabled="runBusy" :loading="terminatingIds.has(row.id)" @click="handleRunClick(row)">{{ isScenarioRunActive(row) ? '终止' : '运行' }}</el-button><el-dropdown trigger="click"><el-button text :icon="MoreFilled" :aria-label="row.name + ' 的更多操作'" /><template #dropdown><el-dropdown-menu><el-dropdown-item @click="handleEdit(row.id)">编辑</el-dropdown-item><el-dropdown-item @click="handleReport(row)">查看报告</el-dropdown-item><el-dropdown-item divided :disabled="!canDeleteScenario(row)" :title="deletePermissionTip(row)" @click="handleDelete(row)">删除</el-dropdown-item></el-dropdown-menu></template></el-dropdown></div></template></el-table-column>
                    <template #empty><el-empty description="暂无场景" :image-size="56" /></template>
                </el-table>
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
            </el-main>
        </el-container>

        <!-- Run Configuration Dialog -->
        <el-dialog v-model="runDialogVisible" title="运行配置" width="400px" :close-on-click-modal="!runBusy" :close-on-press-escape="!runBusy" :show-close="!runBusy">
            <el-form :disabled="runBusy" :model="runForm" label-width="100px">
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
                                    <el-tag :type="deviceStatusTagType(dev.status)" size="small">{{ deviceStatusLabel(dev.status) }}</el-tag>
                                    <span v-if="deviceUnavailableReason(dev)" style="font-size: 12px; color: var(--ad-warning);">
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
                    <el-select :disabled="runBusy" v-model="runForm.envId" placeholder="选择环境 (可选)" clearable style="width: 100%">
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
                    <el-button :disabled="runBusy" @click="runDialogVisible = false">取消</el-button>
                    <el-button type="primary" :loading="runBusy" :disabled="runBusy" @click="confirmRun">{{ runPhase === 'prechecking' ? '预检中' : runPhase === 'submitting' ? '启动中' : '开始执行' }}</el-button>
                </div>
            </template>
        </el-dialog>
    </div>

    <el-drawer
        v-if="isMobileMode"
        v-model="runDialogVisible"
        title="运行配置"
        direction="btt"
        :close-on-click-modal="!runBusy" :close-on-press-escape="!runBusy" :show-close="!runBusy"
        size="82%"
    >
        <div class="mobile-run-form">
            <label class="mobile-run-label">目标设备</label>
            <el-checkbox-group :disabled="runBusy" v-model="runForm.deviceSerials" class="mobile-device-checks">
                <el-checkbox
                    v-for="dev in devices"
                    :key="dev.serial"
                    :label="dev.serial"
                    :disabled="!isDeviceSelectable(dev)"
                    class="mobile-device-check"
                >
                    <div class="mobile-device-check-content">
                        <span>{{ dev.custom_name || dev.market_name || dev.model || dev.serial }}</span>
                        <el-tag :type="deviceStatusTagType(dev.status)" size="small">{{ deviceStatusLabel(dev.status) }}</el-tag>
                    </div>
                    <small v-if="deviceUnavailableReason(dev)">{{ deviceUnavailableReason(dev) }}</small>
                </el-checkbox>
            </el-checkbox-group>
            <div v-if="hasWdaDownDevice" class="run-warning-hint">
                检测到 iOS 设备 WDA 异常，需在设备中心先执行“检测WDA”。
            </div>

            <label class="mobile-run-label">运行环境</label>
            <el-select :disabled="runBusy" v-model="runForm.envId" placeholder="选择环境 (可选)" clearable style="width: 100%">
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
                <el-button :disabled="runBusy" @click="runDialogVisible = false">取消</el-button>
                <el-button type="primary" :loading="runBusy" :disabled="runBusy" @click="confirmRun">{{ runPhase === 'prechecking' ? '预检中' : runPhase === 'submitting' ? '启动中' : '开始执行' }}</el-button>
            </div>
        </template>
    </el-drawer>
</template>

<style scoped>
.scenario-list-container {
    height: 100%;
    display: flex;
    flex-direction: column;
    background: var(--ad-bg);
}

.main-layout {
    flex: 1;
    overflow: hidden;
    margin: 10px;
    gap: 10px;
}

.folder-aside {
    background: var(--ad-surface);
    border-radius: 4px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

.list-main {
    padding: 0 !important;
    overflow: hidden;
    display: flex;
    flex-direction: column;
}

.content-wrapper {
    flex: 1;
    min-height: 0;
    background: var(--ad-surface);
    border-radius: 4px;
    display: flex;
    flex-direction: column;
    padding: 12px;
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
    background: var(--ad-surface); /* Maintained white for items inside (card in card is fine, or maybe make items simpler?) CaseList uses table rows. Here we use cards. */
    /* Let's keep cards but make them stand out less or change background of list area? 
       Actually, if background is white, cards should have border or different bg?
       CaseList has white bg and table rows.
       Here we have cards. 
       Let's keep cards but add border. 
    */
    background: var(--ad-surface);
    border-radius: 6px;
    margin-bottom: 12px;
    border: 1px solid var(--ad-border);
    position: relative;
    overflow: hidden; /* For status strip */
    transition: all 0.2s ease;
    align-items: center;
}

.scenario-item:hover {
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
    border-color: var(--ad-border);
    transform: none;
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
    font-size: 14px;
    font-weight: 600;
    color: var(--ad-text);
    cursor: pointer;
}
.scenario-name:hover {
    color: var(--ad-primary);
}

.step-badge {
    font-weight: normal;
    color: var(--ad-muted);
    border-color: var(--ad-border);
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
.status-text.success { color: var(--ad-success); }
.status-text.warning { color: var(--ad-warning); }
.status-text.failure { color: var(--ad-danger); }
.status-text.running { color: var(--ad-primary); }
.status-text.neutral { color: var(--ad-muted); }

.row-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: var(--ad-muted);
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
    color: var(--ad-warning);
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
    border-left: 1px solid var(--ad-bg); /* Subtle separator */
    height: 70%;
}

.duration-badge {
    font-size: 12px;
    color: var(--ad-muted);
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
    font-size: 14px;
}

.action-btn-edit {
    font-size: 14px;
    color: var(--ad-muted);
}
.action-btn-edit:hover {
    color: var(--ad-primary);
}

.more-icon {
    font-size: 14px;
    color: var(--ad-muted);
    cursor: pointer;
    padding: 4px;
    transform: rotate(90deg);
}
.more-icon:hover {
    color: var(--ad-primary);
}

/* Custom Scrollbar for list area */
.list-scroll-area::-webkit-scrollbar {
    width: 6px;
}
.list-scroll-area::-webkit-scrollbar-thumb {
    background: var(--ad-border);
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
    background: var(--ad-bg);
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
    border: 1px solid var(--ad-border);
    border-radius: 8px;
    background: var(--ad-surface);
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
    color: var(--ad-text);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.mobile-scenario-info-line {
    margin-top: 6px;
    display: flex;
    align-items: center;
    gap: 7px;
    flex-wrap: wrap;
    min-width: 0;
    overflow: hidden;
    font-size: 14px;
    color: var(--ad-muted);
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
    color: var(--ad-muted);
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
    font-size: 14px;
}

.mobile-run-form :deep(.el-select__placeholder),
.mobile-run-form :deep(.el-input__inner) {
    font-size: 16px;
}

.mobile-run-label {
    font-size: 14px;
    font-weight: 600;
    color: var(--ad-text);
}

.mobile-device-checks {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.mobile-device-check {
    margin-right: 0;
    border: 1px solid var(--ad-border);
    border-radius: 8px;
    padding: 10px;
    background: var(--ad-surface);
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
    font-size: 14px;
    margin-top: 4px;
    color: var(--ad-warning);
}

.mobile-drawer-footer {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
}

.mobile-drawer-footer .el-button {
    margin-left: 0;
}

/* Compact desktop workspace: row density comes from structure, not zoom. */
.main-layout { margin: 16px; gap: 12px; min-height: 0; }
.folder-aside { border: 1px solid var(--ad-border); border-radius: var(--ad-panel-radius); }
.content-wrapper { padding: 0; border: 1px solid var(--ad-border); border-radius: var(--ad-panel-radius); }
.toolbar, .list-header { min-height: 48px; padding: 8px 12px; margin: 0; gap: 8px; flex-wrap: nowrap; border-bottom: 1px solid var(--ad-border); }
.left-tools, .right-tools, .left-filters, .right-actions { gap: 8px; display: flex; align-items: center; min-width: 0; }
.search-input { width: 190px; }
.pagination-footer { padding: 8px 12px; margin: 0; min-height: 44px; border-top: 1px solid var(--ad-border); flex-shrink: 0; }
.ad-table { font-size: 12px; }
.ad-table :deep(.el-table__cell) { height: 36px; padding: 0; }
.ad-table :deep(.cell) { line-height: 20px; padding: 0 10px; }
.ad-table :deep(.el-button) { min-height: 28px; height: 28px; font-size: 12px; }
.ad-name-button { font: inherit; color: var(--ad-text); font-weight: 500; background: none; border: 0; padding: 0; cursor: pointer; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ad-name-button:hover { color: var(--ad-primary); }
.ad-row-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
.metadata-trigger { color: var(--ad-muted); }
.list-scroll-area { min-height: 0; overflow: hidden; }

.mobile-case-page, .mobile-scenario-page, .mobile-run-form { font-size: 14px; }
.mobile-case-page :deep(.el-button), .mobile-scenario-page :deep(.el-button), .mobile-device-check, .mobile-drawer-footer :deep(.el-button) { min-height: 44px; font-size: 14px; }
.mobile-case-page :deep(input), .mobile-scenario-page :deep(input), .mobile-run-form :deep(input), .mobile-run-form :deep(.el-select__placeholder) { font-size: 16px; }
.mobile-case-page :deep(.el-tag), .mobile-scenario-page :deep(.el-tag), .mobile-run-form :deep(.el-tag), .mobile-run-form :deep(.el-checkbox__label), .mobile-run-form .run-warning-hint { font-size: 14px; }
.mobile-status-filter :deep(.el-radio-button__inner) { min-height: 44px; display: flex; align-items: center; font-size: 14px; }
.mobile-pagination :deep(button), .mobile-pagination :deep(.el-pager li) { min-width: 44px; height: 44px; font-size: 14px; }
.mobile-page-position { padding: 0 12px; font-size: 14px; color: var(--ad-muted); }
</style>
