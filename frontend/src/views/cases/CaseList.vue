<script setup>
import { ref, onActivated, reactive, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, VideoPlay, CopyDocument, Delete, Search, Refresh, Edit, ArrowDown, FolderAdd, EditPen, Document, FolderOpened } from '@element-plus/icons-vue'
import api from '@/api'
import dayjs from 'dayjs'

const router = useRouter()

// ==================== Folder Tree ====================
const folderTree = ref([])
const selectedFolderId = ref(null)
const treeRef = ref(null)
const renamingFolderId = ref(null)
const renamingValue = ref('')
const renameInputRef = ref(null)

const treeProps = {
    children: 'children',
    label: 'name',
    isLeaf: (data) => data.type === 'case'
}

const fetchFolderTree = async () => {
    try {
        const res = await api.getFolderTree()
        const { tree, all_cases } = res.data
        const allNode = { id: 'all', name: '所有用例', type: 'all', children: all_cases || [] }
        folderTree.value = [allNode, ...tree]
    } catch (err) {
        console.error('Failed to load folder tree:', err)
    }
}

const handleNodeClick = (data) => {
    if (data.type === 'case') {
        router.push(`/ui/cases/${data.case_id}/edit`)
        return
    }
    selectedFolderId.value = data.type === 'all' ? null : data.folder_id
    currentPage.value = 1
    fetchCases()
}

// ---- Drag & Drop ----
const allowDrag = (draggingNode) => {
    return draggingNode.data.type === 'case'
}

const allowDrop = (draggingNode, dropNode, type) => {
    if (dropNode.data.type === 'case') return type === 'none'
    if (dropNode.data.type === 'all') return false
    return type === 'inner'
}

const handleNodeDrop = async (draggingNode, dropNode) => {
    const caseId = draggingNode.data.case_id
    const targetFolderId = dropNode.data.folder_id
    if (!caseId || !targetFolderId) return
    try {
        await api.moveCase(caseId, targetFolderId)
        ElMessage.success('用例已移动')
        fetchFolderTree()
        fetchCases()
    } catch (err) {
        ElMessage.error('移动失败: ' + (err.response?.data?.detail || err.message))
        fetchFolderTree()
    }
}

const handleCreateRootFolder = async () => {
    try {
        const { value } = await ElMessageBox.prompt('请输入目录名称', '新建根目录', {
            confirmButtonText: '创建',
            cancelButtonText: '取消',
            inputPattern: /\S+/,
            inputErrorMessage: '目录名不能为空'
        })
        await api.createFolder({ name: value, parent_id: null })
        ElMessage.success('目录已创建')
        fetchFolderTree()
    } catch {}
}

const handleCreateSubFolder = async (parentData) => {
    try {
        const { value } = await ElMessageBox.prompt('请输入子目录名称', `在「${parentData.name}」下新建`, {
            confirmButtonText: '创建',
            cancelButtonText: '取消',
            inputPattern: /\S+/,
            inputErrorMessage: '目录名不能为空'
        })
        await api.createFolder({ name: value, parent_id: parentData.folder_id })
        ElMessage.success('子目录已创建')
        fetchFolderTree()
    } catch {}
}

const startRename = (data) => {
    renamingFolderId.value = data.folder_id
    renamingValue.value = data.name
    nextTick(() => {
        renameInputRef.value?.focus()
    })
}

const confirmRename = async (data) => {
    if (!renamingValue.value.trim()) {
        renamingFolderId.value = null
        return
    }
    try {
        await api.renameFolder(data.folder_id, { name: renamingValue.value.trim() })
        ElMessage.success('已重命名')
        fetchFolderTree()
    } catch (err) {
        ElMessage.error('重命名失败: ' + (err.response?.data?.detail || err.message))
    }
    renamingFolderId.value = null
}

const handleDeleteFolder = (data) => {
    ElMessageBox.confirm(`确定要删除目录「${data.name}」吗？`, '警告', {
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        type: 'warning'
    }).then(async () => {
        try {
            await api.deleteFolder(data.folder_id)
            ElMessage.success('目录已删除')
            if (selectedFolderId.value === data.folder_id) {
                selectedFolderId.value = null
                fetchCases()
            }
            fetchFolderTree()
        } catch (err) {
            ElMessage.error(err.response?.data?.detail || '删除失败')
        }
    }).catch(() => {})
}

// ==================== Case Table ====================
const cases = ref([])
const loading = ref(false)
const selectedCases = ref([])
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(20)

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

const hasWdaDownDevice = () => devices.value.some(d => d.status === 'WDA_DOWN')

const normalizeCaseRunStatus = (status) => {
    const normalized = (status || '').toString().trim().toLowerCase()
    if (normalized === 'pass' || normalized === 'success') return 'PASS'
    if (normalized === 'fail' || normalized === 'failed' || normalized === 'error') return 'FAIL'
    if (normalized === 'warning') return 'WARNING'
    if (normalized === 'running' || normalized === 'running...') return 'RUNNING'
    return ''
}

const caseRunStatusTagType = (status) => {
    const normalized = normalizeCaseRunStatus(status)
    if (normalized === 'PASS') return 'success'
    if (normalized === 'WARNING') return 'warning'
    if (normalized === 'FAIL') return 'danger'
    if (normalized === 'RUNNING') return 'info'
    return 'info'
}

const handleRunClick = (row) => {
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

        const promises = runnable.map(serial =>
            api.runTestCaseAsync(runningCaseId.value, runForm.envId, serial)
        )
        await Promise.all(promises)

        if (blocked.length > 0) {
            const first = blocked[0]
            ElMessage.warning(`已在 ${runnable.length} 台设备启动；${blocked.length} 台预检失败（示例：${first.serial} - ${first.reason}）`)
        } else {
            ElMessage.success(`已在 ${runForm.deviceSerials.length} 台设备上开始后台执行`)
        }
        runDialogVisible.value = false
        // Update the item status optimistically
        const caseItem = cases.value.find(c => c.id === runningCaseId.value)
        if (caseItem) caseItem.last_run_status = 'RUNNING'
    } catch (err) {
        ElMessage.error('启动批量执行失败: ' + err.message)
    }
}

const handleClone = async (row) => {
    try {
        await api.duplicateTestCase(row.id)
        ElMessage.success('用例已克隆')
        fetchCases()
        fetchFolderTree()
    } catch (err) {
        ElMessage.error('克隆用例失败: ' + err.message)
    }
}

const handleDelete = (row) => {
    ElMessageBox.confirm('确定要删除该测试用例吗？', '警告', {
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        type: 'warning',
    }).then(async () => {
        try {
            await api.deleteTestCase(row.id)
            ElMessage.success('已删除')
            fetchCases()
            fetchFolderTree()
        } catch (err) {
            ElMessage.error('删除失败: ' + err.message)
        }
    })
}

const handleBatchDelete = () => {
    if (selectedCases.value.length === 0) return
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
            fetchFolderTree()
        } catch (err) {
            ElMessage.error('批量删除部分失败: ' + err.message)
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
    fetchFolderTree()
    fetchCases()
})
</script>

<template>
    <div class="case-list-container">
        <el-container class="main-layout">
            <!-- Left: Folder Tree -->
            <el-aside width="200px" class="folder-aside">
                <div class="aside-header">
                    <span class="aside-title">用例目录</span>
                    <el-tooltip content="新建根目录" placement="top">
                        <el-button :icon="FolderAdd" size="small" type="primary" link @click="handleCreateRootFolder" />
                    </el-tooltip>
                </div>

                <div class="tree-wrapper">
                    <el-tree
                        ref="treeRef"
                        :data="folderTree"
                        :props="treeProps"
                        node-key="id"
                        highlight-current
                        :default-expanded-keys="[]"
                        :expand-on-click-node="false"
                        draggable
                        :allow-drag="allowDrag"
                        :allow-drop="allowDrop"
                        @node-click="handleNodeClick"
                        @node-drop="handleNodeDrop"
                    >
                        <template #default="{ node, data }">
                            <div class="tree-node" :class="{ 'is-case-node': data.type === 'case' }">
                                <!-- Rename mode (folder only) -->
                                <template v-if="renamingFolderId === data.folder_id && data.type === 'folder'">
                                    <el-input
                                        ref="renameInputRef"
                                        v-model="renamingValue"
                                        size="small"
                                        style="width: 120px"
                                        @keyup.enter="confirmRename(data)"
                                        @blur="confirmRename(data)"
                                    />
                                </template>
                                <!-- Case node -->
                                <template v-else-if="data.type === 'case'">
                                    <el-icon class="node-icon case-icon"><Document /></el-icon>
                                    <span class="tree-node-label case-label">{{ data.name }}</span>
                                </template>
                                <!-- Folder / All node -->
                                <template v-else>
                                    <div class="env-item-left">
                                        <el-icon :size="16" class="env-icon"><FolderOpened /></el-icon>
                                        <span class="env-name">{{ data.name }}</span>
                                    </div>
                                    <span v-if="data.type === 'folder'" class="node-actions env-item-actions" @click.stop>
                                        <el-button :icon="FolderAdd" size="small" link type="primary" title="新增子目录" @click="handleCreateSubFolder(data)" />
                                        <el-button :icon="EditPen" size="small" link type="primary" title="重命名" @click="startRename(data)" />
                                        <el-button :icon="Delete" size="small" link type="danger" title="删除" @click="handleDeleteFolder(data)" />
                                    </span>
                                </template>
                            </div>
                        </template>
                    </el-tree>
                </div>
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
                            <el-button type="danger" plain :icon="Delete" :disabled="selectedCases.length === 0" @click="handleBatchDelete">
                                批量删除
                            </el-button>
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
                                        v-if="normalizeCaseRunStatus(row.last_run_status)"
                                        :type="caseRunStatusTagType(row.last_run_status)"
                                        size="small"
                                    >
                                        {{ normalizeCaseRunStatus(row.last_run_status) }}
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
                                    <el-tooltip content="后台运行" placement="top">
                                        <el-button :icon="VideoPlay" link type="success" @click="handleRunClick(row)" />
                                    </el-tooltip>
                                    <el-tooltip content="编辑" placement="top">
                                        <el-button :icon="Edit" link type="primary" @click="handleEdit(row)" />
                                    </el-tooltip>
                                    <el-tooltip content="克隆" placement="top">
                                        <el-button :icon="CopyDocument" link type="primary" @click="handleClone(row)" />
                                    </el-tooltip>
                                    <el-tooltip content="删除" placement="top">
                                        <el-button :icon="Delete" link type="danger" @click="handleDelete(row)" />
                                    </el-tooltip>
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
                                    <el-tag :type="statusTagType(dev.status)" size="small">{{ statusLabel(dev.status) }}</el-tag>
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

.aside-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 14px 16px;
    border-bottom: 1px solid #ebeef5;
    flex-shrink: 0;
}

.aside-title {
    font-size: 14px;
    font-weight: 600;
    color: #303133;
}

.tree-wrapper {
    flex: 1;
    overflow-y: auto;
    padding: 8px 0;
}

:deep(.el-tree-node__content) {
    height: 38px;
    border-radius: 8px;
    margin-bottom: 4px;
    padding-right: 8px !important;
    transition: all 0.2s ease;
    border: 1px solid transparent;
}

:deep(.el-tree-node__content:hover) {
    background: #f5f7fa;
}

:deep(.el-tree-node.is-current > .el-tree-node__content) {
    background-color: #ecf5ff;
    border: 1px solid #b3d8ff;
}

.tree-node {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 13px;
    overflow: hidden;
    padding-right: 4px;
    min-width: 0;
}

/* New custom variables styles */
.env-item-left {
    display: flex;
    align-items: center;
    gap: 8px;
    overflow: hidden;
    flex: 1;
    min-width: 0;
}

.env-icon {
    color: #409eff;
    flex-shrink: 0;
}

.env-name {
    font-size: 13px;
    color: #303133;
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.env-item-actions {
    display: flex;
    gap: 2px;
    opacity: 0;
    transition: opacity 0.2s;
    flex-shrink: 0;
}

.tree-node:hover .env-item-actions {
    opacity: 1;
}

/* Existing case node inner spacing styles */
.tree-node-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.is-case-node {
    cursor: pointer;
    justify-content: flex-start;
}

.node-icon {
    flex-shrink: 0;
    margin-right: 4px;
}

.case-icon {
    color: #909399;
    font-size: 13px;
}

.case-label {
    color: #606266;
    font-weight: 400;
}

.is-case-node:hover .case-label {
    color: #409eff;
}

:deep(.el-tree__drop-indicator) {
    display: none;
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
</style>
