<template>
  <div class="split-container" :class="{ 'is-mobile-mode': isMobileMode }">
    <aside class="brand-panel" aria-label="AutoDroid 测试工作台">
      <div class="brand-mark"><span>A</span>AutoDroid</div>
      <div class="brand-copy"><p class="eyebrow">AUTOMATION WORKSPACE</p><h1>让每一次发布，<br />都有可靠的答案。</h1><p>连接设备、编排测试、追踪结果。<br />团队的自动化工作，在这里有序展开。</p></div>
      <div class="brand-footer"><span class="brand-dot" />统一的自动化测试工作台</div>
    </aside>

    <!-- 右侧：登录表单 -->
    <div class="right-panel">
      <div class="mode-switch-wrap">
        <ClientModeSwitch />
      </div>
      <div class="form-wrapper">
        <div class="form-header">
          <p class="form-brand">AutoDroid</p><h2 class="title">登录工作台</h2>
          <p class="subtitle">欢迎回来，继续你的测试工作。</p>
        </div>

        <el-form
          ref="loginFormRef"
          :model="loginForm"
          :rules="loginRules"
          class="login-form"
          label-position="top"
          scroll-to-error
          @submit.prevent
          @keyup.enter="handleLogin"
        >
          <el-form-item label="用户名" prop="username">
            <el-input
              v-model="loginForm.username" autocomplete="username"
              placeholder="用户名"
              :prefix-icon="User"
              size="default"
              class="minimal-input"
            />
          </el-form-item>

          <el-form-item label="密码" prop="password">
            <el-input
              v-model="loginForm.password" autocomplete="current-password"
              type="password"
              placeholder="密码"
              :prefix-icon="Lock"
              show-password
              size="default"
              class="minimal-input"
            />
          </el-form-item>

          <el-form-item>
            <el-button
              :loading="loading"
              class="submit-btn"
              type="primary"
              @click="handleLogin"
            >
              登录
            </el-button>
          </el-form-item>
        </el-form>

        <div class="form-footer">
          <router-link v-if="allowRegistration" to="/register" class="register-link">没有账号？去注册</router-link>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '../stores/useUserStore'
import { User, Lock } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import api, { LOGIN_NOTICE_KEY } from '@/api'
import { describeError } from '@/utils/errors'
import ClientModeSwitch from '@/components/ClientModeSwitch.vue'
import { useClientMode } from '@/composables/useClientMode'

const router = useRouter()
const userStore = useUserStore()
const { isMobileMode } = useClientMode()
const loginFormRef = ref(null)
const loading = ref(false)
const allowRegistration = ref(true)

const loginForm = reactive({
  username: '',
  password: ''
})

const loginRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
}

const loadRegistrationStatus = async () => {
  try {
    const res = await api.getRegistrationStatus()
    allowRegistration.value = res.data?.allow_registration !== false
  } catch (error) {
    allowRegistration.value = true
  }
}

const handleLogin = async () => {
  if (!loginFormRef.value || loading.value) return

  await loginFormRef.value.validate(async (valid) => {
    if (valid) {
      loading.value = true
      try {
        await userStore.login(loginForm.username, loginForm.password)
        ElMessage.success('登录成功')
        router.push('/')
      } catch (error) {
        ElMessage.error(describeError(error) || '登录失败，请检查账号密码')
      } finally {
        loading.value = false
      }
    }
  })
}

onMounted(() => {
  loadRegistrationStatus()
  // Set by the API layer when a request was rejected as unauthenticated.
  const notice = sessionStorage.getItem(LOGIN_NOTICE_KEY)
  if (notice) { sessionStorage.removeItem(LOGIN_NOTICE_KEY); ElMessage.warning(notice) }
})
</script>

<style scoped src="./account/auth.css"></style>
