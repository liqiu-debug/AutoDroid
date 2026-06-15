<script setup>
import { onMounted, reactive, ref } from 'vue'
import dayjs from 'dayjs'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CirclePlus, Refresh, Search, User, Lock, Message } from '@element-plus/icons-vue'
import api from '@/api'
import { useUserStore } from '@/stores/useUserStore'

const userStore = useUserStore()

const loading = ref(false)
const savingRegistration = ref(false)
const creating = ref(false)
const createDialogVisible = ref(false)
const searchKeyword = ref('')
const allowRegistration = ref(true)
const users = ref([])
const createFormRef = ref(null)

const createForm = reactive({
  username: '',
  full_name: '',
  email: '',
  initial_password: '',
})

const createRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  initial_password: [
    { required: true, message: '请输入初始密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' },
  ],
}

const formatDate = (value) => {
  return value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '-'
}

const isCurrentUser = (row) => {
  return row.id === userStore.userInfo?.id
}

const loadRegistrationSettings = async () => {
  const res = await api.getAdminRegistrationSettings()
  allowRegistration.value = res.data?.allow_registration !== false
}

const loadUsers = async () => {
  const params = {}
  const keyword = searchKeyword.value.trim()
  if (keyword) {
    params.search = keyword
  }
  const res = await api.getAdminUsers(params)
  users.value = res.data || []
}

const loadData = async () => {
  loading.value = true
  try {
    await Promise.all([loadRegistrationSettings(), loadUsers()])
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '加载用户管理数据失败')
  } finally {
    loading.value = false
  }
}

const handleRegistrationChange = async (value) => {
  savingRegistration.value = true
  try {
    await api.updateAdminRegistrationSettings(value)
    ElMessage.success('注册开关已更新')
  } catch (error) {
    allowRegistration.value = !value
    ElMessage.error(error.response?.data?.detail || '更新注册开关失败')
  } finally {
    savingRegistration.value = false
  }
}

const resetCreateForm = () => {
  createForm.username = ''
  createForm.full_name = ''
  createForm.email = ''
  createForm.initial_password = ''
  createFormRef.value?.clearValidate()
}

const openCreateDialog = () => {
  resetCreateForm()
  createDialogVisible.value = true
}

const handleCreateUser = async () => {
  if (!createFormRef.value) return

  await createFormRef.value.validate(async (valid) => {
    if (!valid) return

    creating.value = true
    try {
      await api.createAdminUser({
        username: createForm.username,
        full_name: createForm.full_name,
        email: createForm.email,
        initial_password: createForm.initial_password,
      })
      ElMessage.success('用户已新增')
      createDialogVisible.value = false
      resetCreateForm()
      await loadUsers()
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '新增用户失败')
    } finally {
      creating.value = false
    }
  })
}

const handleToggleStatus = async (row) => {
  const nextActive = !row.is_active
  const actionText = nextActive ? '恢复' : '停用'

  try {
    await ElMessageBox.confirm(`确定要${actionText}用户「${row.username}」吗？`, '提示', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch (error) {
    return
  }

  try {
    const res = await api.updateAdminUserStatus(row.id, nextActive)
    Object.assign(row, res.data)
    ElMessage.success(`用户已${actionText}`)
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || `${actionText}用户失败`)
  }
}

onMounted(loadData)
</script>

<template>
  <div class="admin-page" v-loading="loading">
    <div class="page-header">
      <h2>用户管理</h2>
      <div class="header-actions">
        <el-button :icon="Refresh" @click="loadData">刷新</el-button>
        <el-button type="primary" :icon="CirclePlus" @click="openCreateDialog">新增用户</el-button>
      </div>
    </div>

    <section class="settings-panel">
      <div class="setting-row">
        <div>
          <div class="setting-title">允许公开注册</div>
          <div class="setting-value">{{ allowRegistration ? '已开启' : '已关闭' }}</div>
        </div>
        <el-switch
          v-model="allowRegistration"
          :loading="savingRegistration"
          active-text="开启"
          inactive-text="关闭"
          @change="handleRegistrationChange"
        />
      </div>
    </section>

    <section class="table-panel">
      <div class="table-toolbar">
        <el-input
          v-model="searchKeyword"
          placeholder="搜索用户名或姓名"
          clearable
          class="search-input"
          :prefix-icon="Search"
          @keyup.enter="loadUsers"
          @clear="loadUsers"
        />
        <el-button :icon="Search" @click="loadUsers">搜索</el-button>
      </div>

      <el-table :data="users" row-key="id" height="100%" class="user-table">
        <el-table-column label="用户名" prop="username" min-width="160">
          <template #default="{ row }">
            <div class="user-cell">
              <span>{{ row.username }}</span>
              <el-tag v-if="isCurrentUser(row)" size="small" type="info" effect="plain">当前账号</el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="姓名" prop="full_name" min-width="150">
          <template #default="{ row }">{{ row.full_name || '-' }}</template>
        </el-table-column>
        <el-table-column label="邮箱" prop="email" min-width="220">
          <template #default="{ row }">{{ row.email || '-' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" effect="plain">
              {{ row.is_active ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button
              link
              :type="row.is_active ? 'danger' : 'primary'"
              :disabled="isCurrentUser(row) && row.is_active"
              @click="handleToggleStatus(row)"
            >
              {{ row.is_active ? '停用' : '恢复' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <el-dialog
      v-model="createDialogVisible"
      title="新增用户"
      width="460px"
      @closed="resetCreateForm"
    >
      <el-form ref="createFormRef" :model="createForm" :rules="createRules" label-position="top">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="createForm.username" :prefix-icon="User" autocomplete="off" />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="createForm.full_name" :prefix-icon="User" autocomplete="off" />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="createForm.email" :prefix-icon="Message" autocomplete="off" />
        </el-form-item>
        <el-form-item label="初始密码" prop="initial_password">
          <el-input
            v-model="createForm.initial_password"
            type="password"
            show-password
            :prefix-icon="Lock"
            autocomplete="new-password"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="handleCreateUser">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.admin-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 24px;
  background: #f2f3f5;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.page-header h2 {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
  color: #1f2933;
}

.header-actions {
  display: flex;
  gap: 10px;
}

.settings-panel,
.table-panel {
  border: 1px solid #dcdfe6;
  border-radius: 8px;
  background: #ffffff;
}

.settings-panel {
  margin-bottom: 16px;
  padding: 18px 20px;
}

.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.setting-title {
  font-size: 15px;
  font-weight: 600;
  color: #1f2933;
}

.setting-value {
  margin-top: 4px;
  font-size: 13px;
  color: #606266;
}

.table-panel {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 16px;
}

.table-toolbar {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-bottom: 12px;
}

.search-input {
  width: 260px;
}

.user-table {
  flex: 1;
  min-height: 0;
}

.user-cell {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

@media (max-width: 720px) {
  .admin-page {
    padding: 16px;
  }

  .page-header,
  .setting-row,
  .table-toolbar {
    align-items: stretch;
    flex-direction: column;
  }

  .header-actions,
  .search-input {
    width: 100%;
  }
}
</style>
