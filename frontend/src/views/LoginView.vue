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
          <el-form-item v-if="isClientApp" label="服务器地址" prop="serverAddress">
            <el-input
              v-model="loginForm.serverAddress"
              placeholder="http://127.0.0.1:8000 或 https://autodroid.example.com"
              :prefix-icon="Connection"
              size="default"
              class="minimal-input"
            />
          </el-form-item>

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

          <el-form-item v-if="isClientApp" class="login-options">
            <el-checkbox v-model="rememberInfo" @change="onRememberInfoChange">保存信息</el-checkbox>
            <el-checkbox v-model="autoLogin" :disabled="!rememberInfo">自动登录</el-checkbox>
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
          <!-- 客户端（桌面引擎）模式无注册链路（缺 X-AutoDroid-Server/upstream），入口隐藏；
               注册入口 clientProbeDone 门控：探测落定前不渲染，探测失败维持隐藏（请联系管理员开号） -->
          <router-link v-if="clientProbeDone && allowRegistration && !isClientApp && !probeFailed" to="/register" class="register-link">没有账号？去注册</router-link>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '../stores/useUserStore'
import { User, Lock, Connection } from '@element-plus/icons-vue'
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
// 客户端（桌面引擎）模式：GET /api/client/info 返回 200 才启用；
// 404/失败视为纯 Web 模式，UI 与登录行为与原先完全一致。
const isClientApp = ref(false)
// 注册入口 clientProbeDone 门控：探测落定前不渲染注册入口，探测失败维持「请联系管理员开号」（隐藏入口）。
const clientProbeDone = ref(false)
// 探测失败（非 404，无法判定注册通道）：不放行注册入口。
const probeFailed = ref(false)
const rememberInfo = ref(false)
const autoLogin = ref(false)

const loginForm = reactive({
  serverAddress: '',
  username: '',
  password: ''
})

// 普通常量且始终含 serverAddress 规则：引用永不变化，避免 el-form
// validateOnRuleChange 在客户端探测翻转 isClientApp 时首屏误报必填；
// 纯 Web 模式下 serverAddress 的 form-item 未挂载，该规则天然不生效。
const loginRules = {
  serverAddress: [{ required: true, message: '请输入服务器地址', trigger: 'blur' }],
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

// 自动登录依赖保存信息：未勾选保存信息时自动登录同时失效并置灰。
const onRememberInfoChange = (value) => {
  if (!value) autoLogin.value = false
}

const detectClientApp = async () => {
  try {
    const res = await api.getClientInfo()
    isClientApp.value = res?.data?.clientMode === true
    probeFailed.value = false
  } catch (error) {
    // 404 = 明确无客户端端点 → 纯 Web 模式；其余（网络/5xx）无法判定 → 维持隐藏注册入口（请联系管理员开号）。
    if (error?.response?.status === 404) {
      isClientApp.value = false
      probeFailed.value = false
    } else {
      isClientApp.value = false
      probeFailed.value = true
    }
  } finally {
    clientProbeDone.value = true
  }
  if (!isClientApp.value) return null
  try {
    const saved = await api.getSavedLogin()
    if (!saved) return null
    loginForm.serverAddress = saved.server || ''
    loginForm.username = saved.username || ''
    loginForm.password = saved.password || ''
    rememberInfo.value = saved.rememberInfo === true
    autoLogin.value = rememberInfo.value && saved.autoLogin === true
    return saved
  } catch {
    return null
  }
}

const persistLogin = async () => {
  // 约定：保存信息=全部保存（含密码，由 preload 用 safeStorage 加密落盘）；未勾选=清空保存。
  if (!isClientApp.value) return
  try {
    if (rememberInfo.value) {
      await api.saveLogin({
        server: loginForm.serverAddress.trim(),
        username: loginForm.username,
        password: loginForm.password,
        rememberInfo: true,
        autoLogin: autoLogin.value === true
      })
    } else {
      await api.saveLogin(null)
    }
  } catch {
    // 凭据保存失败不影响登录结果，但必须让用户知情（不再静默吞掉）
    ElMessage.warning('登录成功，但保存登录信息失败')
  }
}

const handleLogin = async () => {
  if (!loginFormRef.value || loading.value) return

  const valid = await Promise.resolve(loginFormRef.value.validate()).catch(() => false)
  if (valid === false) return

  loading.value = true
  try {
    await userStore.login(loginForm.username, loginForm.password, {
      server: isClientApp.value ? loginForm.serverAddress.trim() : undefined
    })
    await persistLogin()
    ElMessage.success('登录成功')
    router.push('/')
  } catch (error) {
    ElMessage.error(describeError(error) || '登录失败，请检查账号密码')
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  loadRegistrationStatus()
  // Set by the API layer when a request was rejected as unauthenticated.
  const notice = sessionStorage.getItem(LOGIN_NOTICE_KEY)
  if (notice) { sessionStorage.removeItem(LOGIN_NOTICE_KEY); ElMessage.warning(notice) }
  const saved = await detectClientApp()
  // 自动登录：仅当保存过信息且勾选过自动登录时，回填凭据并直接提交登录。
  // 手动退出登录后（loggedOut）跨重启也不再自动登录，直到重新手动登录并勾选自动登录
  // （markLoggedOut 已同时清掉 autoLogin，此处是双保险门控）。
  if (saved && saved.rememberInfo === true && saved.autoLogin === true && saved.loggedOut !== true) {
    await handleLogin()
  }
})
</script>

<style scoped src="./account/auth.css"></style>
