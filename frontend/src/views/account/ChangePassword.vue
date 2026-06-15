<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft, Lock } from '@element-plus/icons-vue'
import api from '@/api'

const router = useRouter()
const formRef = ref(null)
const saving = ref(false)

const form = reactive({
  current_password: '',
  new_password: '',
  confirm_password: '',
})

const validateConfirmPassword = (rule, value, callback) => {
  if (!value) {
    callback(new Error('请再次输入新密码'))
  } else if (value !== form.new_password) {
    callback(new Error('两次输入密码不一致'))
  } else {
    callback()
  }
}

const rules = {
  current_password: [{ required: true, message: '请输入当前密码', trigger: 'blur' }],
  new_password: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' },
  ],
  confirm_password: [{ validator: validateConfirmPassword, trigger: 'blur' }],
}

const resetForm = () => {
  form.current_password = ''
  form.new_password = ''
  form.confirm_password = ''
  formRef.value?.clearValidate()
}

const handleSubmit = async () => {
  if (!formRef.value) return

  await formRef.value.validate(async (valid) => {
    if (!valid) return

    saving.value = true
    try {
      await api.changePassword({
        current_password: form.current_password,
        new_password: form.new_password,
      })
      ElMessage.success('密码已更新')
      resetForm()
    } catch (error) {
      ElMessage.error(error.response?.data?.detail || '修改密码失败')
    } finally {
      saving.value = false
    }
  })
}
</script>

<template>
  <div class="account-page">
    <div class="page-header">
      <el-button :icon="ArrowLeft" text @click="router.back()">返回</el-button>
      <h2>修改密码</h2>
    </div>

    <section class="password-panel">
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        class="password-form"
        @keyup.enter="handleSubmit"
      >
        <el-form-item label="当前密码" prop="current_password">
          <el-input
            v-model="form.current_password"
            type="password"
            show-password
            :prefix-icon="Lock"
            autocomplete="current-password"
          />
        </el-form-item>

        <el-form-item label="新密码" prop="new_password">
          <el-input
            v-model="form.new_password"
            type="password"
            show-password
            :prefix-icon="Lock"
            autocomplete="new-password"
          />
        </el-form-item>

        <el-form-item label="确认新密码" prop="confirm_password">
          <el-input
            v-model="form.confirm_password"
            type="password"
            show-password
            :prefix-icon="Lock"
            autocomplete="new-password"
          />
        </el-form-item>

        <div class="form-actions">
          <el-button @click="resetForm">重置</el-button>
          <el-button type="primary" :loading="saving" @click="handleSubmit">保存</el-button>
        </div>
      </el-form>
    </section>
  </div>
</template>

<style scoped>
.account-page {
  height: 100%;
  overflow: auto;
  padding: 24px;
  background: #f2f3f5;
}

.page-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.page-header h2 {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
  color: #1f2933;
}

.password-panel {
  max-width: 520px;
  padding: 24px;
  border: 1px solid #dcdfe6;
  border-radius: 8px;
  background: #ffffff;
}

.password-form {
  max-width: 420px;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 8px;
}
</style>
