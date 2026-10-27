<template>
  <div class="split-container" :class="{ 'is-mobile-mode': isMobileMode }">
    <aside class="brand-panel" aria-label="AutoDroid 测试工作台">
      <div class="brand-mark"><span>A</span>AutoDroid</div>
      <div class="brand-copy"><p class="eyebrow">AUTOMATION WORKSPACE</p><h1>让每一次发布，<br />都有可靠的答案。</h1><p>连接设备、编排测试、追踪结果。<br />团队的自动化工作，在这里有序展开。</p></div>
      <div class="brand-footer"><span class="brand-dot" />统一的自动化测试工作台</div>
    </aside>

    <!-- 右侧：注册表单 -->
    <div class="right-panel">
      <div class="mode-switch-wrap">
        <ClientModeSwitch />
      </div>
      <div class="form-wrapper">
        <div class="form-header">
          <p class="form-brand">AutoDroid</p><h2 class="title">创建账号</h2>
          <p class="subtitle">开始与团队一起构建可靠的测试。</p>
        </div>

        <!-- 注册入口 clientProbeDone 门控：探测落定前不渲染注册入口/提示，避免误闪 -->
        <el-alert
          v-if="clientProbeDone && isClientApp"
          title="客户端模式请通过平台开号，请联系管理员开号。"
          type="warning"
          show-icon
          :closable="false"
          class="registration-alert"
        />

        <el-alert
          v-else-if="clientProbeDone && probeFailed"
          title="暂时无法确认注册通道，请联系管理员开号。"
          type="warning"
          show-icon
          :closable="false"
          class="registration-alert"
        />

        <el-alert
          v-else-if="clientProbeDone && !registrationAllowed"
          title="当前已关闭公开注册，请联系管理员创建账号。"
          type="warning"
          show-icon
          :closable="false"
          class="registration-alert"
        />

        <el-form
          v-if="clientProbeDone && registrationAllowed && !isClientApp && !probeFailed"
          ref="formRef"
          :model="form"
          :rules="rules"
          class="login-form"
          label-position="top"
          scroll-to-error
          @submit.prevent
          @keyup.enter="handleRegister"
        >
          <el-form-item label="用户名" prop="username">
            <el-input
              v-model="form.username" autocomplete="username"
              placeholder="用户名"
              :prefix-icon="User"
              size="default"
              class="minimal-input"
            />
          </el-form-item>

          <el-form-item label="姓名" prop="name">
            <el-input
              v-model="form.name" autocomplete="name"
              placeholder="真实姓名"
              :prefix-icon="Avatar"
              size="default"
              class="minimal-input"
            />
          </el-form-item>

          <el-form-item label="密码" prop="password">
            <el-input
              v-model="form.password" autocomplete="new-password"
              type="password"
              placeholder="密码"
              :prefix-icon="Lock"
              show-password
              size="default"
              class="minimal-input"
            />
          </el-form-item>

          <el-form-item label="确认密码" prop="confirmPassword">
            <el-input
              v-model="form.confirmPassword" autocomplete="new-password"
              type="password"
              placeholder="确认密码"
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
              @click="handleRegister"
            >
              立刻注册
            </el-button>
          </el-form-item>
        </el-form>

        <div class="form-footer">
          <router-link to="/login" class="register-link">已有账号？去登录</router-link>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { User, Lock, Avatar } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import ClientModeSwitch from '@/components/ClientModeSwitch.vue'
import { useClientMode } from '@/composables/useClientMode'

const router = useRouter()
const { isMobileMode } = useClientMode()
const formRef = ref(null)
const loading = ref(false)
const registrationAllowed = ref(true)
// 客户端（桌面引擎）模式：GET /api/client/info 返回 200 才启用；
// 该模式下注册链路不可用（无 X-AutoDroid-Server/upstream），提示平台开号并阻止提交。
const isClientApp = ref(false)
// 注册入口 clientProbeDone 门控：探测落定前不渲染注册入口，探测失败维持「请联系管理员开号」。
const clientProbeDone = ref(false)
// 探测失败（非 404，无法判定注册通道）：不放行注册，维持「请联系管理员开号」。
const probeFailed = ref(false)

const detectClientApp = async () => {
  try {
    const res = await api.getClientInfo()
    isClientApp.value = res?.data?.clientMode === true
    probeFailed.value = false
  } catch (error) {
    // 404 = 明确无客户端端点 → 纯 Web 模式；其余（网络/5xx）无法判定 → 维持「请联系管理员开号」。
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
}

const form = reactive({
  username: '',
  name: '',
  password: '',
  confirmPassword: ''
})

const validatePass2 = (rule, value, callback) => {
  if (value === '') {
    callback(new Error('请再次输入密码'))
  } else if (value !== form.password) {
    callback(new Error('两次输入密码不一致!'))
  } else {
    callback()
  }
}

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  name: [{ required: true, message: '请输入真实姓名', trigger: 'blur' }],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' }
  ],
  confirmPassword: [{ validator: validatePass2, trigger: 'blur' }]
}

const loadRegistrationStatus = async () => {
  try {
    const res = await api.getRegistrationStatus()
    registrationAllowed.value = res.data?.allow_registration !== false
  } catch (error) {
    registrationAllowed.value = true
  }
}

const handleRegister = async () => {
  if (isClientApp.value) {
    ElMessage.warning('客户端模式请通过平台开号，请联系管理员开号')
    return
  }
  if (probeFailed.value) {
    ElMessage.warning('暂时无法确认注册通道，请联系管理员开号')
    return
  }
  if (!registrationAllowed.value) {
    ElMessage.warning('当前已关闭公开注册，请联系管理员创建账号')
    return
  }
  if (!formRef.value || loading.value) return

  await formRef.value.validate(async (valid) => {
    if (valid) {
      loading.value = true
      try {
        await api.register({
            username: form.username,
            name: form.name,
            password: form.password
        })
        ElMessage.success('注册成功，请登录')
        router.push('/login')
      } catch (error) {
        ElMessage.error(error.response?.data?.detail || '注册失败')
      } finally {
        loading.value = false
      }
    }
  })
}

onMounted(async () => {
  await detectClientApp()
  await loadRegistrationStatus()
})
</script>

<style scoped src="../account/auth.css"></style>
