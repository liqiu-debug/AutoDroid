<script setup>
import { ref, onMounted, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Plus, Delete, Edit, Search, FolderOpened, Key, Hide, View
} from '@element-plus/icons-vue'
import api from '@/api'

// ==================== 环境状态 ====================
const environments = ref([])
const activeEnvId = ref(null)
const envLoading = ref(false)

// ==================== 变量状态 ====================
const variables = ref([])
const varLoading = ref(false)
const searchKeyword = ref('')

// ==================== 环境弹窗 ====================
const envDialogVisible = ref(false)
const envForm = ref({ name: '', description: '' })
const editingEnv = ref(null)

// ==================== 变量弹窗 ====================
const varDialogVisible = ref(false)
const varForm = ref({ key: '', value: '', is_secret: false, description: '' })
const editingVar = ref(null)
const varFormRef = ref(null)

const varFormRules = {
  key: [
    { required: true, message: '请输入变量 Key', trigger: 'blur' },
    { pattern: /^[A-Z0-9_]+$/, message: 'Key 仅允许大写字母、数字和下划线', trigger: 'blur' }
  ]
}

const activeEnv = computed(() => environments.value.find(e => e.id === activeEnvId.value))

const filteredVariables = computed(() => {
  if (!searchKeyword.value) return variables.value
  const kw = searchKeyword.value.toUpperCase()
  return variables.value.filter(v =>
    v.key.includes(kw) || (v.description || '').toUpperCase().includes(kw)
  )
})

// ==================== 环境方法 ====================
const fetchEnvironments = async () => {
  envLoading.value = true
  try {
    const { data } = await api.getEnvironments()
    environments.value = data
    if (data.length && !activeEnvId.value) {
      activeEnvId.value = data[0].id
    }
    if (activeEnvId.value) {
      await fetchVariables()
    }
  } catch (e) {
    ElMessage.error('获取环境列表失败')
  } finally {
    envLoading.value = false
  }
}

const handleSelectEnv = async (envId) => {
  activeEnvId.value = envId
  searchKeyword.value = ''
  await fetchVariables()
}

const openEnvDialog = (env = null) => {
  editingEnv.value = env
  envForm.value = env
    ? { name: env.name, description: env.description || '' }
    : { name: '', description: '' }
  envDialogVisible.value = true
}

const submitEnv = async () => {
  if (!envForm.value.name.trim()) {
    ElMessage.warning('请输入环境名称')
    return
  }
  try {
    if (editingEnv.value) {
      await api.updateEnvironment(editingEnv.value.id, envForm.value)
      ElMessage.success('环境已更新')
    } else {
      await api.createEnvironment(envForm.value)
      ElMessage.success('环境已创建')
    }
    envDialogVisible.value = false
    await fetchEnvironments()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '操作失败')
  }
}

const handleDeleteEnv = async (env) => {
  try {
    await ElMessageBox.confirm(
      `确定要删除环境「${env.name}」吗？该环境下的所有变量也将被删除。`,
      '确认删除',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
    await api.deleteEnvironment(env.id)
    ElMessage.success('环境已删除')
    if (activeEnvId.value === env.id) {
      activeEnvId.value = null
      variables.value = []
    }
    await fetchEnvironments()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(e.response?.data?.detail || '删除失败')
  }
}

// ==================== 变量方法 ====================
const fetchVariables = async () => {
  if (!activeEnvId.value) return
  varLoading.value = true
  try {
    const { data } = await api.getVariables(activeEnvId.value)
    variables.value = data
  } catch (e) {
    ElMessage.error('获取变量列表失败')
  } finally {
    varLoading.value = false
  }
}

const openVarDialog = (v = null) => {
  editingVar.value = v
  varForm.value = v
    ? { key: v.key, value: v.value, is_secret: v.is_secret, description: v.description || '' }
    : { key: '', value: '', is_secret: false, description: '' }
  varDialogVisible.value = true
}

const submitVar = async () => {
  if (varFormRef.value) {
    try { await varFormRef.value.validate() } catch { return }
  }
  try {
    if (editingVar.value) {
      await api.updateVariable(editingVar.value.id, varForm.value)
      ElMessage.success('变量已更新')
    } else {
      await api.createVariable(activeEnvId.value, varForm.value)
      ElMessage.success('变量已创建')
    }
    varDialogVisible.value = false
    await fetchVariables()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '操作失败')
  }
}

const handleDeleteVar = async (v) => {
  try {
    await ElMessageBox.confirm(
      `确定要删除变量「${v.key}」吗？`,
      '确认删除',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
    await api.deleteVariable(v.id)
    ElMessage.success('变量已删除')
    await fetchVariables()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(e.response?.data?.detail || '删除失败')
  }
}

// ==================== 密文可见切换 ====================
const revealedIds = ref(new Set())
const toggleReveal = (id) => {
  if (revealedIds.value.has(id)) {
    revealedIds.value.delete(id)
  } else {
    revealedIds.value.add(id)
  }
}

/** 变量引用格式，避免模板中写 {{ 导致解析错误 */
const refFormat = (key) => `{{ ${key} }}`

// ==================== 生命周期 ====================
onMounted(() => {
  fetchEnvironments()
})
</script>

<template>
  <div class="variable-library">
    <el-container class="main-container">
      <!-- ========== 左侧：环境导航 ========== -->
      <el-aside width="260px" class="env-aside">
        <div class="aside-header">
          <span class="aside-title">环境列表</span>
          <el-button type="primary" :icon="Plus" size="small" @click="openEnvDialog()">新增</el-button>
        </div>

        <div class="env-list" v-loading="envLoading">
          <div
            v-for="env in environments"
            :key="env.id"
            class="env-item"
            :class="{ active: activeEnvId === env.id }"
            @click="handleSelectEnv(env.id)"
          >
            <div class="env-item-left">
              <el-icon :size="16" class="env-icon"><FolderOpened /></el-icon>
              <span class="env-name">{{ env.name }}</span>
            </div>
            <div class="env-item-actions" @click.stop>
              <el-button :icon="Edit" size="small" link @click="openEnvDialog(env)" />
              <el-button :icon="Delete" size="small" link type="danger" @click="handleDeleteEnv(env)" />
            </div>
          </div>
          <el-empty v-if="!envLoading && environments.length === 0" description="暂无环境，请新建" :image-size="60" />
        </div>
      </el-aside>

      <!-- ========== 右侧：变量表格 ========== -->
      <el-main class="var-main">
        <!-- 顶部工具栏 -->
        <div class="var-toolbar">
          <div class="toolbar-left">
            <el-icon :size="22" color="#409eff"><Key /></el-icon>
            <h2 class="page-title">{{ activeEnv?.name || '全局变量库' }}</h2>
            <el-tag v-if="activeEnv" type="info" size="small" style="margin-left: 12px;">
              {{ variables.length }} 个变量
            </el-tag>
          </div>
          <div class="toolbar-right">
            <el-input
              v-model="searchKeyword"
              :prefix-icon="Search"
              placeholder="搜索 Key / 描述"
              clearable
              size="default"
              style="width: 220px;"
              :disabled="!activeEnvId"
            />
            <el-button type="primary" :icon="Plus" @click="openVarDialog()" :disabled="!activeEnvId">
              新增变量
            </el-button>
          </div>
        </div>

        <!-- 变量表格 -->
        <el-table
          v-if="activeEnvId"
          :data="filteredVariables"
          v-loading="varLoading"
          stripe
          style="width: 100%"
          empty-text="暂无变量，请点击「新增变量」按钮"
        >
          <el-table-column prop="key" label="Key" min-width="180">
            <template #default="{ row }">
              <span class="var-key">{{ row.key }}</span>
            </template>
          </el-table-column>
          <el-table-column label="Value" min-width="240">
            <template #default="{ row }">
              <div class="value-cell">
                <span v-if="row.is_secret && !revealedIds.has(row.id)" class="secret-mask">******</span>
                <span v-else class="var-value">{{ row.value }}</span>
                <el-button
                  v-if="row.is_secret"
                  :icon="revealedIds.has(row.id) ? Hide : View"
                  size="small"
                  link
                  @click="toggleReveal(row.id)"
                  class="reveal-btn"
                />
              </div>
            </template>
          </el-table-column>
          <el-table-column label="密文" width="80" align="center">
            <template #default="{ row }">
              <el-tag :type="row.is_secret ? 'danger' : 'info'" size="small" effect="plain">
                {{ row.is_secret ? '是' : '否' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="description" label="描述" min-width="180" show-overflow-tooltip />
          <el-table-column label="引用方式" width="180">
            <template #default="{ row }">
              <el-tag type="success" size="small" effect="plain" class="ref-tag">{{ refFormat(row.key) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="120" align="center" fixed="right">
            <template #default="{ row }">
              <el-button :icon="Edit" size="small" link type="primary" @click="openVarDialog(row)">编辑</el-button>
              <el-button :icon="Delete" size="small" link type="danger" @click="handleDeleteVar(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>

        <!-- 未选中环境 -->
        <el-empty v-else description="请在左侧选择或新建一个环境" :image-size="120" style="margin-top: 80px;" />
      </el-main>
    </el-container>

    <!-- ========== 环境弹窗 ========== -->
    <el-dialog
      v-model="envDialogVisible"
      :title="editingEnv ? '编辑环境' : '新建环境'"
      width="480px"
      destroy-on-close
    >
      <el-form :model="envForm" label-width="80px">
        <el-form-item label="环境名称" required>
          <el-input v-model="envForm.name" placeholder="例如：开发环境 / 生产环境" maxlength="50" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="envForm.description" type="textarea" :rows="3" placeholder="可选" maxlength="200" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="envDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitEnv">确定</el-button>
      </template>
    </el-dialog>

    <!-- ========== 变量弹窗 ========== -->
    <el-dialog
      v-model="varDialogVisible"
      :title="editingVar ? '编辑变量' : '新增变量'"
      width="520px"
      destroy-on-close
    >
      <el-form ref="varFormRef" :model="varForm" :rules="varFormRules" label-width="80px">
        <el-form-item label="Key" prop="key">
          <el-input
            v-model="varForm.key"
            placeholder="大写字母、数字和下划线，如 API_BASE_URL"
            maxlength="100"
            :disabled="!!editingVar"
          />
        </el-form-item>
        <el-form-item label="Value">
          <el-input v-model="varForm.value" placeholder="变量值" maxlength="2000" />
        </el-form-item>
        <el-form-item label="密文">
          <el-switch v-model="varForm.is_secret" />
          <span class="switch-hint">开启后，变量值在列表中将显示为 ******</span>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="varForm.description" type="textarea" :rows="2" placeholder="可选" maxlength="200" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="varDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitVar">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.variable-library {
  height: 100%;
  overflow: hidden;
  background: linear-gradient(135deg, #f5f7fa 0%, #e4e7ed 100%);
}

.main-container {
  height: 100%;
}

/* ========== 左侧环境面板 ========== */
.env-aside {
  background: #fff;
  border-right: 1px solid #e4e7ed;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.aside-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 16px 12px;
  border-bottom: 1px solid #ebeef5;
  flex-shrink: 0;
}

.aside-title {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
}

.env-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.env-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 12px;
  margin-bottom: 4px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s ease;
}

.env-item:hover {
  background: #f5f7fa;
}

.env-item.active {
  background: #ecf5ff;
  border: 1px solid #b3d8ff;
}

.env-item:not(.active) {
  border: 1px solid transparent;
}

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

.env-item:hover .env-item-actions {
  opacity: 1;
}

/* ========== 右侧变量区域 ========== */
.var-main {
  padding: 20px 24px;
  overflow-y: auto;
}

.var-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  padding: 16px 20px;
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.06);
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.page-title {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  color: #303133;
}

/* ========== 表格样式 ========== */
.var-key {
  font-family: 'SF Mono', 'Menlo', 'Monaco', monospace;
  font-size: 13px;
  font-weight: 600;
  color: #409eff;
}

.value-cell {
  display: flex;
  align-items: center;
  gap: 6px;
}

.var-value {
  font-family: 'SF Mono', 'Menlo', 'Monaco', monospace;
  font-size: 13px;
  color: #303133;
  word-break: break-all;
}

.secret-mask {
  font-size: 13px;
  color: #909399;
  letter-spacing: 2px;
}

.reveal-btn {
  flex-shrink: 0;
}

.ref-tag {
  font-family: 'SF Mono', 'Menlo', 'Monaco', monospace;
  font-size: 12px;
}

/* ========== 弹窗辅助 ========== */
.switch-hint {
  font-size: 12px;
  color: #909399;
  margin-left: 12px;
}
</style>
