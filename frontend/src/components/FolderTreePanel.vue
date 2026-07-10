<script setup>
import { ref, nextTick, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { FolderAdd, EditPen, Delete, Document, FolderOpened } from '@element-plus/icons-vue'

/**
 * 通用目录树面板（用例/场景列表页共用）。
 * 数据源与 API 通过 props 注入：fetchTree 返回 { tree, items }，
 * 叶子节点由后端目录树接口生成（type 为 'case' / 'scenario' 等）。
 */
const props = defineProps({
    /** 面板标题，如「用例目录」 */
    title: { type: String, required: true },
    /** 根节点文案，如「所有用例」 */
    allLabel: { type: String, required: true },
    /** 叶子节点上的业务 ID 字段名，如 'case_id' / 'scenario_id' */
    itemIdKey: { type: String, required: true },
    /** async () => ({ tree: [...], items: [...] }) */
    fetchTree: { type: Function, required: true },
    /** async ({ name, parent_id }) */
    createFolder: { type: Function, required: true },
    /** async (folderId, { name }) */
    renameFolder: { type: Function, required: true },
    /** async (folderId) */
    deleteFolder: { type: Function, required: true },
    /** async (itemId, folderId) 将叶子移动到目录 */
    moveItem: { type: Function, required: true }
})

const emit = defineEmits(['select-folder', 'open-item', 'item-moved'])

const treeData = ref([])
const treeRef = ref(null)
const selectedFolderId = ref(null)
const renamingFolderId = ref(null)
const renamingValue = ref('')
const renameInputRef = ref(null)

const treeProps = {
    children: 'children',
    label: 'name',
    isLeaf: (data) => data.type !== 'folder' && data.type !== 'all'
}

const isLeafNode = (data) => data.type !== 'folder' && data.type !== 'all'

const refresh = async () => {
    try {
        const { tree, items } = await props.fetchTree()
        const allNode = { id: 'all', name: props.allLabel, type: 'all', children: items || [] }
        treeData.value = [allNode, ...(tree || [])]
    } catch (err) {
        console.error('Failed to load folder tree:', err)
    }
}

const handleNodeClick = (data) => {
    if (isLeafNode(data)) {
        emit('open-item', data)
        return
    }
    selectedFolderId.value = data.type === 'all' ? null : data.folder_id
    emit('select-folder', selectedFolderId.value)
}

// ---- Drag & Drop ----
const allowDrag = (draggingNode) => isLeafNode(draggingNode.data)

const allowDrop = (draggingNode, dropNode, type) => {
    if (dropNode.data.type !== 'folder') return false
    return type === 'inner'
}

const handleNodeDrop = async (draggingNode, dropNode) => {
    const itemId = draggingNode.data[props.itemIdKey]
    const targetFolderId = dropNode.data.folder_id
    if (!itemId || !targetFolderId) return
    try {
        await props.moveItem(itemId, targetFolderId)
        ElMessage.success('已移动')
        refresh()
        emit('item-moved', { itemId, folderId: targetFolderId })
    } catch (err) {
        ElMessage.error('移动失败: ' + (err.response?.data?.detail || err.message))
        refresh()
    }
}

// ---- Folder CRUD ----
const handleCreateRootFolder = async () => {
    try {
        const { value } = await ElMessageBox.prompt('请输入目录名称', '新建根目录', {
            confirmButtonText: '创建',
            cancelButtonText: '取消',
            inputPattern: /\S+/,
            inputErrorMessage: '目录名不能为空'
        })
        await props.createFolder({ name: value, parent_id: null })
        ElMessage.success('目录已创建')
        refresh()
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
        await props.createFolder({ name: value, parent_id: parentData.folder_id })
        ElMessage.success('子目录已创建')
        refresh()
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
        await props.renameFolder(data.folder_id, { name: renamingValue.value.trim() })
        ElMessage.success('已重命名')
        refresh()
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
            await props.deleteFolder(data.folder_id)
            ElMessage.success('目录已删除')
            if (selectedFolderId.value === data.folder_id) {
                selectedFolderId.value = null
                emit('select-folder', null)
            }
            refresh()
        } catch (err) {
            ElMessage.error(err.response?.data?.detail || '删除失败')
        }
    }).catch(() => {})
}

onMounted(refresh)

defineExpose({ refresh })
</script>

<template>
    <div class="folder-tree-panel">
        <div class="aside-header">
            <span class="aside-title">{{ title }}</span>
            <el-tooltip content="新建根目录" placement="top">
                <el-button :icon="FolderAdd" size="small" type="primary" link @click="handleCreateRootFolder" />
            </el-tooltip>
        </div>

        <div class="tree-wrapper">
            <el-tree
                ref="treeRef"
                :data="treeData"
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
                <template #default="{ data }">
                    <div class="tree-node" :class="{ 'is-leaf-node': isLeafNode(data) }">
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
                        <!-- Leaf node (case / scenario) -->
                        <template v-else-if="isLeafNode(data)">
                            <el-icon class="node-icon leaf-icon"><Document /></el-icon>
                            <span class="tree-node-label leaf-label">{{ data.name }}</span>
                        </template>
                        <!-- Folder / All node -->
                        <template v-else>
                            <div class="folder-item-left">
                                <el-icon :size="16" class="folder-icon"><FolderOpened /></el-icon>
                                <span class="folder-name">{{ data.name }}</span>
                            </div>
                            <span v-if="data.type === 'folder'" class="node-actions folder-item-actions" @click.stop>
                                <el-button :icon="FolderAdd" size="small" link type="primary" title="新增子目录" @click="handleCreateSubFolder(data)" />
                                <el-button :icon="EditPen" size="small" link type="primary" title="重命名" @click="startRename(data)" />
                                <el-button :icon="Delete" size="small" link type="danger" title="删除" @click="handleDeleteFolder(data)" />
                            </span>
                        </template>
                    </div>
                </template>
            </el-tree>
        </div>
    </div>
</template>

<style scoped>
.folder-tree-panel {
    height: 100%;
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

:deep(.el-tree__drop-indicator) {
    display: none;
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

.folder-item-left {
    display: flex;
    align-items: center;
    gap: 8px;
    overflow: hidden;
    flex: 1;
    min-width: 0;
}

.folder-icon {
    color: #409eff;
    flex-shrink: 0;
}

.folder-name {
    font-size: 13px;
    color: #303133;
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.folder-item-actions {
    display: flex;
    gap: 2px;
    opacity: 0;
    transition: opacity 0.2s;
    flex-shrink: 0;
}

.tree-node:hover .folder-item-actions {
    opacity: 1;
}

.tree-node-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.is-leaf-node {
    cursor: pointer;
    justify-content: flex-start;
}

.node-icon {
    flex-shrink: 0;
    margin-right: 4px;
}

.leaf-icon {
    color: #909399;
    font-size: 13px;
}

.leaf-label {
    color: #606266;
    font-weight: 400;
}

.is-leaf-node:hover .leaf-label {
    color: #409eff;
}
</style>
