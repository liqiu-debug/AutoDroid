<script setup>
import { ref, onActivated, onDeactivated, onUnmounted, reactive, computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, VideoPlay, CopyDocument, Delete, Search, Refresh, Edit, ArrowDown, CircleClose } from '@element-plus/icons-vue'
import api from '@/api'
import FolderTreePanel from '@/components/FolderTreePanel.vue'
import { deviceStatusLabel, deviceStatusTagType, normalizeRunStatus, runStatusTagType } from '@/utils/statusMeta'
import { useUserStore } from '@/stores/useUserStore'
import dayjs from 'dayjs'
import { useClientMode } from '@/composables/useClientMode'

const router = useRouter()
const userStore = useUserStore()
const { isMobileMode } = useClientMode()

// ==================== Folder Tree ====================
const selectedFolderId = ref(null)
const treePanelRef = ref(null)

const refreshFolderTree = () => treePanelRef.value?.refresh()

const fetchCaseFolderTree = async () => {
    const res = await api.getFolderTree()
    return { tree: res.data.tree || [], items: res.data.all_cases || [] }
}

const handleFolderSelect = (folderId) => {
    selectedFolderId.value = folderId
    currentPage.value = 1
    fetchCases()
}

const handleOpenCase = (node) => {
    router.push(`/ui/cases/${node.case_id}/edit`)
}

// ==================== Case Table ====================
const cases = ref([])
const loading = ref(false)
const selectedCases = ref([])
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(20)
const currentUser = computed(() => userStore.userInfo || {})
const isAdmin = computed(() => currentUser.value.role === 'admin')
const activeCaseRuns = ref({})
let activeRunTimer = null

const queryParams = reactive({
    keyword: ''
})

const errorMsg = ref('')

const fetchCases = async () => {
    loading.value = true
    errorMsg.value = ''
    try {
        const params = {
            keyword: queryParams.keyword,
            skip: (currentPage.value - 1) * pageSize.value,
            limit: pageSize.value
        }
        if (selectedFolderId.value !== null) {
            params.folder_id = selectedFolderId.value
        }
        const res = await api.getTestCases(params)

        let data = res.data.items || []
        total.value = res.data.total || 0

        cases.value = data
        restoreActiveCaseRuns()
    } catch (err) {
        console.error('Fetch error:', err)
        errorMsg.value = err.message || '未知错误'
        ElMessage.error('加载用例失败: ' + errorMsg.value)
    } finally {
        loading.value = false
    }
}

const handleSearch = () => {
    currentPage.value = 1
    fetchCases()
}

const handleSizeChange = (val) => {
    pageSize.value = val
    currentPage.value = 1
    fetchCases()
}

const handleCurrentChange = (val) => {
    currentPage.value = val
    fetchCases()
}

const handleSelectionChange = (val) => {
    selectedCases.value = val
}

const canDeleteCase = (row) => {
    if (!row) return false
    if (isAdmin.value) return true
    return row.user_id !== null && row.user_id !== undefined && row.user_id === currentUser.value.id
}

const deletePermissionTip = (row) => {
    return canDeleteCase(row) ? '删除' : '仅创建人或管理员可以删除'
}

const selectedHasUnauthorizedCase = computed(() => {
    return selectedCases.value.some(item => !canDeleteCase(item))
})

const handleCreate = () => {
    const query = {}
    if (selectedFolderId.value !== null) {
        query.folder_id = selectedFolderId.value
    }
    router.push({ path: '/ui/cases/create', query })
}

const handleEdit = (row) => {
    router.push(`/ui/cases/${row.id}/edit`)
}

// ==================== Run Configuration ====================
const runDialogVisible = ref(false)
const runningCaseId = ref(null)
const runForm = reactive({
    envId: null,
    deviceSerials: []
})
const environments = ref([])
const devices = ref([])

const summarizePrecheckFailure = (payload) => {
    if (!payload || typeof payload !== 'object') return '预检失败'
    const globalFail = (payload.global_checks || []).find(item => item?.status === 'FAIL')
    if (globalFail) return globalFail.message || globalFail.code || '全局检查失败'

    const stepFail = (payload.steps || []).find(item => item?.status === 'FAIL')
    if (stepFail) return stepFail.message || stepFail.code || '步骤预检失败'

    if (!payload.has_runnable_steps) return '全部步骤将被跳过（当前设备无可执行步骤）'
    return '预检失败'
}

const precheckCaseOnDevice = async (caseId, serial) => {
    try {
        const { data } = await api.precheckTestCase(caseId, runForm.envId, serial)
        if (data?.ok) return { ok: true }
        return { ok: false, reason: summarizePrecheckFailure(data) }
    } catch (err) {
        const detail = err?.response?.data?.detail || err?.message || '请求失败'
        return { ok: false, reason: `预检接口调用失败: ${detail}` }
    }
}

const fetchRunConfigOptions = async () => {
    try {
        const [envRes, devRes] = await Promise.all([
            api.getEnvironments(),
            api.getDeviceList()
        ])
        environments.value = envRes.data || []
        
        // Extract devices array properly depends on backend format
        let devs = devRes.data || []
        // Sometimes backend returns { devices: [...] }
        if (devs.devices) devs = devs.devices
        else if (devs.items) devs = devs.items
        devices.value = Array.isArray(devs) ? devs : []
        
        if (environments.value.length > 0 && !runForm.envId) {
            runForm.envId = environments.value[0].id
        }
        if (devices.value.length > 0 && runForm.deviceSerials.length === 0) {
            // Find the first IDLE device
            const firstIdle = devices.value.find(d => d.status === 'IDLE')
            if (firstIdle) {
                 runForm.deviceSerials = [firstIdle.serial]
            }
        }
    } catch (err) {
        console.error('获取运行配置选项失败', err)
    }
}

const isDeviceSelectable = (device) => device?.status === 'IDLE'

const deviceUnavailableReason = (device) => {
    if (!device) return ''
    if (device.status === 'WDA_DOWN') return 'WDA 未就绪'
    if (device.status === 'BUSY') return '设备正忙'
    return ''
}

const hasWdaDownDevice = () => devices.value.some(d => d.status === 'WDA_DOWN')

const isCaseRunActive = (row) => Boolean(activeCaseRuns.value[row.id])

const caseQueueInfo = (row) => activeCaseRuns.value[row.id]?.queue || null

const caseStatusText = (row) => {
    const s = normalizeRunStatus(row.last_run_status)
    if (s === 'QUEUED') {
        const queue = caseQueueInfo(row)
        return queue?.position ? `排队中（第 ${queue.position} 位）` : '排队中'
    }
    return s
}

// 从 /runs/active 的 items 构建活跃 run 条目（含排队信息）
const buildActiveCaseEntry = (items, fallback = {}) => {
    const entry = {
        batch_id: items[0]?.batch_id ?? fallback.batch_id,
        run_ids: items.map(item => item.run_id).filter(Boolean),
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
    if (isCaseRunActive(row)) {
        await terminateCaseRun(row)
        return
    }
    runningCaseId.value = row.id
    runDialogVisible.value = true
    // Fetch and retain previous selections if valid, else pick defaults
    fetchRunConfigOptions()
}

const confirmRun = async () => {
    if (!runningCaseId.value) return
    if (!runForm.deviceSerials || runForm.deviceSerials.length === 0) {
        ElMessage.warning('请至少选择一台设备')
        return
    }
    
    try {
        const runnable = []
        const blocked = []
        for (const serial of runForm.deviceSerials) {
            const check = await precheckCaseOnDevice(runningCaseId.value, serial)
            if (check.ok) runnable.push(serial)
            else blocked.push({ serial, reason: check.reason })
        }

        if (runnable.length === 0) {
            const first = blocked[0]
            ElMessage.error(`运行前预检未通过：${first ? `${first.serial} - ${first.reason}` : '无可执行设备'}`)
            return
        }

        const { data } = await api.runTestCaseBatch(runningCaseId.value, runForm.envId, runnable)

        // 并发超限的任务会进入 FIFO 队列而非失败：区分"已开始 / 已加入队列"
        const runs = Array.isArray(data?.runs) ? data.runs : []
        const queuedRuns = runs.filter(item => item.queued)
        const startedCount = runs.length - queuedRuns.length
        const queuePositions = queuedRuns.map(item => item.queue_position).filter(p => Number.isFinite(p))
        const minQueuePosition = queuePositions.length ? Math.min(...queuePositions) : null

        let startMsg
        if (queuedRuns.length === 0) {
            startMsg = `已在 ${runnable.length} 台设备上开始后台执行`
        } else if (startedCount === 0) {
            startMsg = `并发已满，已加入执行队列（${queuedRuns.length} 台${minQueuePosition ? `，最前第 ${minQueuePosition} 位` : ''}）`
        } else {
            startMsg = `已开始 ${startedCount} 台；${queuedRuns.length} 台已加入队列${minQueuePosition ? `（最前第 ${minQueuePosition} 位）` : ''}`
        }

        if (blocked.length > 0) {
            const first = blocked[0]
            ElMessage.warning(`${startMsg}；${blocked.length} 台预检失败（示例：${first.serial} - ${first.reason}）`)
        } else if (queuedRuns.length > 0) {
            ElMessage.info(startMsg)
        } else {
            ElMessage.success(startMsg)
        }
        runDialogVisible.value = false
        // Update the item status optimistically
        const caseItem = cases.value.find(c => c.id === runningCaseId.value)
        if (caseItem) caseItem.last_run_status = startedCount > 0 || queuedRuns.length === 0 ? 'RUNNING' : 'QUEUED'
        const activeEntry = {
            batch_id: data?.batch_id,
            run_ids: data?.run_ids || [],
            device_serials: runnable
        }
        if (queuedRuns.length > 0) {
            activeEntry.queue = { count: queuedRuns.length, position: minQueuePosition }
        }
        activeCaseRuns.value = {
            ...activeCaseRuns.value,
            [runningCaseId.value]: activeEntry
        }
        startActiveRunPolling()
    } catch (err) {
        ElMessage.error('启动批量执行失败: ' + err.message)
    }
}

const terminateCaseRun = async (row) => {
    const active = activeCaseRuns.value[row.id]
    if (!active) return
    try {
        await api.cancelRun({
            kind: 'case',
            target_id: row.id,
            batch_id: active.batch_id || null,
            run_ids: active.run_ids || [],
            device_serials: active.device_serials || []
        })
        ElMessage.warning('已发送终止请求')
        row.last_run_status = 'ABORTED'
        const next = { ...activeCaseRuns.value }
        delete next[row.id]
        activeCaseRuns.value = next
    } catch (err) {
        ElMessage.error('终止失败: ' + (err.response?.data?.detail || err.message))
    }
}

const restoreActiveCaseRuns = async () => {
    const runningRows = cases.value.filter(row => ['RUNNING', 'QUEUED'].includes(normalizeRunStatus(row.last_run_status)))
    if (runningRows.length === 0) return
    const next = { ...activeCaseRuns.value }
    for (const row of runningRows) {
        try {
            const { data } = await api.getActiveRuns('case', row.id)
            const items = data?.items || []
            if (items.length > 0) {
                next[row.id] = buildActiveCaseEntry(items, next[row.id])
            }
        } catch {}
    }
    activeCaseRuns.value = next
    if (Object.keys(next).length > 0) startActiveRunPolling()
}

const startActiveRunPolling = () => {
    stopActiveRunPolling()
    activeRunTimer = setInterval(async () => {
        const entries = Object.entries(activeCaseRuns.value)
        if (entries.length === 0) {
            stopActiveRunPolling()
            return
        }
        const next = { ...activeCaseRuns.value }
        let changed = false
        for (const [caseId] of entries) {
            try {
                const { data } = await api.getActiveRuns('case', Number(caseId))
                const items = data?.items || []
                if (items.length === 0) {
                    delete next[caseId]
                    changed = true
                    continue
                }
                next[caseId] = buildActiveCaseEntry(items, next[caseId])
                // 排队任务获得槽位后（QUEUED -> RUNNING）同步本地行状态
                const caseItem = cases.value.find(c => c.id === Number(caseId))
                if (caseItem && normalizeRunStatus(caseItem.last_run_status) === 'QUEUED' && !next[caseId].queue) {
                    caseItem.last_run_status = 'RUNNING'
                }
            } catch {}
        }
        activeCaseRuns.value = next
        if (changed) fetchCases()
    }, 3000)
}

const stopActiveRunPolling = () => {
    if (activeRunTimer) {
        clearInterval(activeRunTimer)
        activeRunTimer = null
    }
}

const handleClone = async (row) => {
    try {
        await api.duplicateTestCase(row.id)
        ElMessage.success('用例已克隆')
        fetchCases()
        refreshFolderTree()
    } catch (err) {
        ElMessage.error('克隆用例失败: ' + err.message)
    }
}

const handleDelete = (row) => {
    if (!canDeleteCase(row)) {
        ElMessage.warning('仅创建人或管理员可以删除')
        return
    }
    ElMessageBox.confirm('确定要删除该测试用例吗？', '警告', {
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        type: 'warning',
    }).then(async () => {
        try {
            await api.deleteTestCase(row.id)
            ElMessage.success('已删除')
            fetchCases()
            refreshFolderTree()
        } catch (err) {
            ElMessage.error('删除失败: ' + (err.response?.data?.detail || err.message))
        }
    })
}

const handleBatchDelete = () => {
    if (selectedCases.value.length === 0) return
    if (selectedHasUnauthorizedCase.value) {
        ElMessage.warning('仅能删除自己创建的用例')
        return
    }
    ElMessageBox.confirm(`确定要删除选中的 ${selectedCases.value.length} 个用例吗？`, '警告', {
        confirmButtonText: '批量删除',
        cancelButtonText: '取消',
        type: 'warning',
    }).then(async () => {
        loading.value = true
        try {
            for (const item of selectedCases.value) {
                await api.deleteTestCase(item.id)
            }
            ElMessage.success('批量删除成功')
            fetchCases()
            refreshFolderTree()
        } catch (err) {
            ElMessage.error('批量删除部分失败: ' + (err.response?.data?.detail || err.message))
            fetchCases()
        } finally {
            loading.value = false
        }
    })
}

const priorities = ['P0', 'P1', 'P2', 'P3']

const getPriority = (tags) => {
    if (!tags) return ''
    return tags.find(t => priorities.includes(t)) || ''
}

const handlePriorityChange = async (row, newVal) => {
    if (!newVal) return
    const oldTags = row.tags || []
    const newTags = oldTags.filter(t => !priorities.includes(t))
    newTags.push(newVal)
    row.tags = newTags
    try {
        await api.updateTestCase(row.id, {
            name: row.name,
            description: row.description,
            steps: row.steps,
            variables: row.variables,
            tags: newTags,
            folder_id: row.folder_id
        })
        ElMessage.success('优先级已更新')
    } catch (err) {
        ElMessage.error('更新失败: ' + err.message)
        fetchCases()
    }
}

const formatTime = (time) => {
    if (!time) return '-'
    return dayjs(time).format('YYYY-MM-DD HH:mm')
}

onActivated(() => {
    refreshFolderTree()
    fetchCases()
})

onDeactivated(stopActiveRunPolling)
onUnmounted(stopActiveRunPolling)
</script>

<template>
    <div v-if="isMobileMode" class="mobile-case-page">
        <div class="mobile-case-toolbar">
            <el-input
                v-model="queryParams.keyword"
                placeholder="搜索用例..."
                class="mobile-search-input"
                :prefix-icon="Search"
                clearable
                @keyup.enter="handleSearch"
                @clear="handleSearch"
            />
            <el-button :icon="Refresh" circle @click="fetchCases" />
        </div>

        <div class="mobile-case-list" v-loading="loading">
            <article
                v-for="item in cases"
                :key="item.id"
                class="mobile-case-card"
            >
                <div class="mobile-case-card-header">
                    <div class="mobile-case-title">
                        <strong>{{ item.name }}</strong>
                        <span>ID #{{ item.id }}</span>
                    </div>
                    <el-tag
                        v-if="normalizeRunStatus(item.last_run_status)"
                        :type="runStatusTagType(item.last_run_status)"
                        size="small"
                    >
                        {{ caseStatusText(item) }}
                    </el-tag>
                </div>

                <div class="mobile-case-meta">
                    <span>优先级：{{ getPriority(item.tags) || '无' }}</span>
                    <span>更新：{{ formatTime(item.updated_at) }}</span>
                </div>

                <div class="mobile-case-actions">
                    <el-button type="primary" :icon="VideoPlay" @click="handleRunClick(item)">
                        执行用例
                    </el-button>
                </div>
            </article>
            <el-empty v-if="!loading && cases.length === 0" description="暂无用例" :image-size="90" />
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

    <div v-else class="case-list-container">
        <el-container class="main-layout">
            <!-- Left: Folder Tree -->
            <el-aside width="200px" class="folder-aside">
                <FolderTreePanel
                    ref="treePanelRef"
                    title="用例目录"
                    all-label="所有用例"
                    item-id-key="case_id"
                    :fetch-tree="fetchCaseFolderTree"
                    :create-folder="api.createFolder"
                    :rename-folder="api.renameFolder"
                    :delete-folder="api.deleteFolder"
                    :move-item="api.moveCase"
                    @select-folder="handleFolderSelect"
                    @open-item="handleOpenCase"
                    @item-moved="fetchCases"
                />
            </el-aside>

            <!-- Right: Case Table -->
            <el-main class="table-main">
                <div class="content-wrapper">
                    <div class="toolbar">
                        <div class="left-tools">
                            <el-input
                                v-model="queryParams.keyword"
                                placeholder="搜索用例名称..."
                                class="search-input"
                                :prefix-icon="Search"
                                @keyup.enter="handleSearch"
                                clearable
                                @clear="handleSearch"
                            />
                            <el-button :icon="Refresh" @click="fetchCases" circle />
                        </div>

                        <div class="right-tools">
                            <el-tooltip :content="selectedHasUnauthorizedCase ? '仅能删除自己创建的用例' : '批量删除'" placement="top">
                                <span class="button-tooltip-wrap">
                                    <el-button type="danger" plain :icon="Delete" :disabled="selectedCases.length === 0 || selectedHasUnauthorizedCase" @click="handleBatchDelete">
                                        批量删除
                                    </el-button>
                                </span>
                            </el-tooltip>
                            <el-button type="primary" :icon="Plus" @click="handleCreate">新建用例</el-button>
                        </div>
                    </div>

                    <el-alert v-if="errorMsg" :title="errorMsg" type="error" show-icon style="margin-bottom: 20px" />

                    <div class="table-container">
                        <el-table
                            :data="cases"
                            v-loading="loading"
                            style="width: 100%"
                            height="100%"
                            @selection-change="handleSelectionChange"
                            :header-cell-style="{ background: '#f5f7fa', color: '#606266' }"
                        >
                            <el-table-column type="selection" width="55" align="center" />
                            <el-table-column prop="id" label="ID" width="70" align="center" />

                            <el-table-column label="用例名称" min-width="200">
                                <template #default="{ row }">
                                    <span class="case-name" @click="handleEdit(row)">{{ row.name }}</span>
                                </template>
                            </el-table-column>

                            <el-table-column label="优先级" width="100" align="center">
                                <template #default="{ row }">
                                    <el-dropdown trigger="click" @command="(val) => handlePriorityChange(row, val)">
                                        <span class="el-dropdown-link">
                                            <el-tag :type="getPriority(row.tags) === 'P0' ? 'danger' : (getPriority(row.tags) === 'P1' ? 'warning' : 'info')" size="small" class="priority-tag">
                                                <span>{{ getPriority(row.tags) || '无' }}</span>
                                                <el-icon class="el-icon--right"><ArrowDown /></el-icon>
                                            </el-tag>
                                        </span>
                                        <template #dropdown>
                                            <el-dropdown-menu>
                                                <el-dropdown-item command="P0">P0</el-dropdown-item>
                                                <el-dropdown-item command="P1">P1</el-dropdown-item>
                                                <el-dropdown-item command="P2">P2</el-dropdown-item>
                                                <el-dropdown-item command="P3">P3</el-dropdown-item>
                                            </el-dropdown-menu>
                                        </template>
                                    </el-dropdown>
                                </template>
                            </el-table-column>

                            <el-table-column label="状态" width="100" align="center">
                                <template #default="{ row }">
                                    <el-tag
                                        v-if="normalizeRunStatus(row.last_run_status)"
                                        :type="runStatusTagType(row.last_run_status)"
                                        size="small"
                                    >
                                        {{ caseStatusText(row) }}
                                    </el-tag>
                                    <span v-else class="text-gray">-</span>
                                </template>
                            </el-table-column>

                            <el-table-column label="创建信息" width="180" align="center">
                                <template #default="{ row }">
                                    <div class="user-info">
                                        <span>{{ row.creator_name || '-' }}</span>
                                        <span class="time">{{ formatTime(row.created_at) }}</span>
                                    </div>
                                </template>
                            </el-table-column>

                            <el-table-column label="最后更新" width="180" align="center">
                                <template #default="{ row }">
                                    <div class="user-info">
                                        <span>{{ row.updater_name || '-' }}</span>
                                        <span class="time">{{ formatTime(row.updated_at) }}</span>
                                    </div>
                                </template>
                            </el-table-column>

                            <el-table-column label="操作" width="180" align="center" fixed="right">
                                <template #default="{ row }">
                                    <div class="case-action-buttons">
                                        <el-tooltip content="后台运行" placement="top">
                                            <span class="button-tooltip-wrap">
                                                <el-button
                                                    :icon="isCaseRunActive(row) ? CircleClose : VideoPlay"
                                                    link
                                                    :type="isCaseRunActive(row) ? 'danger' : 'success'"
                                                    @click="handleRunClick(row)"
                                                />
                                            </span>
                                        </el-tooltip>
                                        <el-tooltip content="编辑" placement="top">
                                            <span class="button-tooltip-wrap">
                                                <el-button :icon="Edit" link type="primary" @click="handleEdit(row)" />
                                            </span>
                                        </el-tooltip>
                                        <el-tooltip content="克隆" placement="top">
                                            <span class="button-tooltip-wrap">
                                                <el-button :icon="CopyDocument" link type="primary" @click="handleClone(row)" />
                                            </span>
                                        </el-tooltip>
                                        <el-tooltip :content="deletePermissionTip(row)" placement="top">
                                            <span class="button-tooltip-wrap">
                                                <el-button :icon="Delete" link type="danger" :disabled="!canDeleteCase(row)" @click="handleDelete(row)" />
                                            </span>
                                        </el-tooltip>
                                    </div>
                                </template>
                            </el-table-column>
                        </el-table>
                    </div>

                    <div class="pagination-footer">
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
                                    <el-tag :type="deviceStatusTagType(dev.status)" size="small">{{ deviceStatusLabel(dev.status) }}</el-tag>
                                    <span v-if="deviceUnavailableReason(dev)" style="font-size: 12px; color: #e6a23c;">
                                        {{ deviceUnavailableReason(dev) }}
                                    </span>
                                </div>
                            </div>
                        </el-option>
                    </el-select>
                    <div v-if="hasWdaDownDevice()" class="run-warning-hint">
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
        class="mobile-run-drawer"
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
                        <el-tag :type="deviceStatusTagType(dev.status)" size="small">{{ deviceStatusLabel(dev.status) }}</el-tag>
                    </div>
                    <small v-if="deviceUnavailableReason(dev)">{{ deviceUnavailableReason(dev) }}</small>
                </el-checkbox>
            </el-checkbox-group>
            <div v-if="hasWdaDownDevice()" class="run-warning-hint">
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
.case-list-container {
    height: 100%;
    display: flex;
    flex-direction: column;
    background: #f2f3f5;
}

.main-layout {
    flex: 1;
    overflow: hidden;
    margin: 10px;
    gap: 10px;
}

/* ==================== Left Aside ==================== */
.folder-aside {
    background: #fff;
    border-radius: 4px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

.button-tooltip-wrap {
    display: inline-flex;
    align-items: center;
    vertical-align: middle;
    line-height: 1;
}

.case-action-buttons {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 12px;
    line-height: 1;
}

.case-action-buttons :deep(.el-button) {
    margin-left: 0;
}

/* ==================== Right Main ==================== */
.table-main {
    padding: 0 !important;
    overflow: hidden;
    display: flex;
    flex-direction: column;
}

.content-wrapper {
    flex: 1;
    min-height: 0;
    padding: 20px;
    background: #fff;
    border-radius: 4px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

.table-container {
    flex: 1;
    min-height: 0;
    overflow: hidden;
}

:deep(.el-table__inner-wrapper::before) {
    display: none;
}
:deep(.el-table) {
    border-bottom: none;
}

.toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;
    flex-wrap: wrap;
    flex-shrink: 0;
    gap: 15px;
}

.left-tools, .right-tools {
    display: flex;
    align-items: center;
    gap: 10px;
}

.run-warning-hint {
    margin-top: 6px;
    font-size: 12px;
    color: #e6a23c;
}

.search-input {
    width: 220px;
}

.case-name {
    font-weight: 500;
    color: #409eff;
    cursor: pointer;
}
.case-name:hover {
    text-decoration: underline;
}

.user-info {
    display: flex;
    flex-direction: column;
    line-height: 1.4;
    font-size: 13px;
}
.user-info .time {
    font-size: 12px;
    color: #909399;
}

.text-gray { color: #909399; }
.el-dropdown-link { cursor: pointer; display: flex; align-items: center; }
.priority-tag {
    width: 48px;
    padding: 0 4px;
}
.priority-tag :deep(.el-tag__content) {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 2px;
    width: 100%;
    padding-left: 6px;
}

.pagination-footer {
    flex-shrink: 0;
    padding: 12px 10px 0;
    display: flex;
    justify-content: flex-end;
}

.mobile-case-page {
    height: 100%;
    display: flex;
    flex-direction: column;
    background: #f6f7f9;
    padding: 12px;
    box-sizing: border-box;
    overflow: hidden;
}

.mobile-case-toolbar {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 8px;
    margin-bottom: 10px;
}

.mobile-search-input {
    width: 100%;
}

.mobile-case-list {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.mobile-case-card {
    border: 1px solid #ebeef5;
    border-radius: 8px;
    background: #ffffff;
    padding: 14px;
}

.mobile-case-card-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
}

.mobile-case-title {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
}

.mobile-case-title strong {
    font-size: 15px;
    color: #303133;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.mobile-case-title span,
.mobile-case-meta {
    font-size: 12px;
    color: #909399;
}

.mobile-case-meta {
    margin-top: 12px;
    display: flex;
    flex-direction: column;
    gap: 4px;
}

.mobile-case-actions {
    margin-top: 12px;
    display: flex;
}

.mobile-case-actions .el-button {
    flex: 1;
    margin-left: 0;
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
