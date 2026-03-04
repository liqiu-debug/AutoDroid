<template>
  <div class="split-container">
    <!-- 左侧：黑白灰几何拼接 (Geometric Splicing) -->
    <div class="left-panel abstract-grid">
      <div class="grid-item bg-black">
        <div class="decorative-text">AUTODROID</div>
      </div>
      <div class="grid-item bg-white">
        <div class="circle-accent"></div>
      </div>
      <div class="grid-item bg-gray-light"></div>
      <div class="grid-item bg-gray-dark">
        <div class="line-accent"></div>
      </div>
    </div>

    <!-- 右侧：登录表单 -->
    <div class="right-panel">
      <div class="form-wrapper">
        <div class="form-header">
          <h2 class="title">AutoDroid</h2>
          <p class="subtitle">UI自动化测试平台</p>
        </div>
        
        <el-form 
          ref="loginFormRef"
          :model="loginForm"
          :rules="loginRules"
          class="login-form"
          @keyup.enter="handleLogin"
        >
          <el-form-item prop="username">
            <el-input 
              v-model="loginForm.username" 
              placeholder="用户名"
              :prefix-icon="User"
              size="large"
              class="minimal-input"
            />
          </el-form-item>
          
          <el-form-item prop="password">
            <el-input 
              v-model="loginForm.password" 
              type="password" 
              placeholder="密码"
              :prefix-icon="Lock"
              show-password
              size="large"
              class="minimal-input"
            />
          </el-form-item>
          
          <el-form-item>
            <el-button 
              :loading="loading" 
              class="submit-btn" 
              @click="handleLogin"
            >
              登录
            </el-button>
          </el-form-item>
        </el-form>
        
        <div class="form-footer">
          <router-link to="/register" class="register-link">没有账号？去注册</router-link>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '../stores/useUserStore'
import { User, Lock } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

const router = useRouter()
const userStore = useUserStore()
const loginFormRef = ref(null)
const loading = ref(false)

const loginForm = reactive({
  username: '',
  password: ''
})

const loginRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
}

const handleLogin = async () => {
  if (!loginFormRef.value) return
  
  await loginFormRef.value.validate(async (valid) => {
    if (valid) {
      loading.value = true
      try {
        await userStore.login(loginForm.username, loginForm.password)
        ElMessage.success('登录成功')
        router.push('/')
      } catch (error) {
        ElMessage.error(error.response?.data?.detail || '登录失败，请检查账号密码')
      } finally {
        loading.value = false
      }
    }
  })
}
</script>

<style scoped>
.split-container {
  display: flex;
  height: 100vh;
  width: 100vw;
  background-color: #ffffff;
  overflow: hidden;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}

/* --- 左侧拼接设计 --- */
.left-panel {
  flex: 1;
  display: none; /* 移动端隐藏 */
}
@media (min-width: 900px) {
  .left-panel {
    display: block;
  }
}

.abstract-grid {
  display: grid;
  grid-template-columns: 55% 45%;
  grid-template-rows: 35% 45% 20%;
  height: 100%;
  width: 100%;
}

.bg-black {
  background-color: #09090b;
  grid-column: 1 / 2;
  grid-row: 1 / 3;
  position: relative;
}

.bg-white {
  background-color: #ffffff;
  grid-column: 2 / 3;
  grid-row: 1 / 2;
  display: flex;
  align-items: center;
  justify-content: center;
}

.bg-gray-light {
  background-color: #f4f4f5;
  grid-column: 2 / 3;
  grid-row: 2 / 4;
}

.bg-gray-dark {
  background-color: #27272a;
  grid-column: 1 / 2;
  grid-row: 3 / 4;
  position: relative;
}

/* 装饰元素 */
.circle-accent {
  width: 64px;
  height: 64px;
  border-radius: 50%;
  background-color: #09090b;
}

.decorative-text {
  position: absolute;
  bottom: 40px;
  right: -32px;
  color: #ffffff;
  font-size: 64px;
  font-weight: 800;
  letter-spacing: -2px;
  transform: rotate(-90deg);
  transform-origin: bottom right;
  opacity: 0.1;
  user-select: none;
}

.line-accent {
  position: absolute;
  top: 50%;
  left: 0;
  width: 30%;
  height: 2px;
  background-color: #ffffff;
  opacity: 0.8;
}

/* --- 右侧表单 --- */
.right-panel {
  flex: 1;
  max-width: 600px;
  display: flex;
  align-items: center;
  justify-content: center;
  background-color: #ffffff;
  padding: 40px;
}

.form-wrapper {
  width: 100%;
  max-width: 400px;
  background-color: #f4f4f5;
  padding: 40px;
  border-radius: 16px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.04);
  border: 1px solid #e4e4e7;
  transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  position: relative;
}

.form-wrapper::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 3px;
  background-color: transparent;
  transition: background-color 0.3s ease;
  border-top-left-radius: 16px;
  border-top-right-radius: 16px;
}

.form-wrapper:hover {
  box-shadow: 0 16px 48px rgba(0, 0, 0, 0.08);
  transform: translateY(-2px);
  border-color: #e4e4e7;
}

.form-wrapper:hover::before {
  background-color: #09090b;
}

.form-header {
  margin-bottom: 32px;
}

.title {
  font-size: 32px;
  font-weight: 700;
  color: #000000;
  margin: 0 0 8px 0;
  letter-spacing: -0.5px;
}

.subtitle {
  font-size: 15px;
  color: #71717a;
  margin: 0;
}

.login-form {
  margin-top: 20px;
}

/* 输入框定制 */
:deep(.minimal-input .el-input__wrapper) {
  box-shadow: 0 0 0 1px #e4e4e7 inset !important;
  border-radius: 6px;
  background-color: #fafafa;
  padding: 0 15px;
  transition: all 0.2s ease;
}

:deep(.minimal-input .el-input__wrapper.is-focus),
:deep(.minimal-input .el-input__wrapper:hover) {
  box-shadow: 0 0 0 1px #09090b inset !important;
  background-color: #ffffff;
}

:deep(.minimal-input .el-input__inner) {
  font-size: 15px;
  height: 48px;
  color: #09090b;
}

:deep(.minimal-input .el-input__prefix-inner) {
  color: #a1a1aa;
}
:deep(.minimal-input .el-input__wrapper.is-focus .el-input__prefix-inner) {
  color: #09090b;
}

/* 按钮定制 */
.submit-btn {
  width: 100%;
  height: 48px;
  background-color: #09090b !important;
  border: none !important;
  border-radius: 6px;
  color: #ffffff !important;
  font-size: 15px;
  font-weight: 500;
  margin-top: 10px;
  transition: all 0.2s ease;
}

.submit-btn:hover {
  background-color: #27272a !important;
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
}

.submit-btn:active {
  transform: translateY(0);
}

.form-footer {
  margin-top: 40px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}

.info-text {
  font-size: 13px;
  color: #a1a1aa;
  margin: 0;
}

.register-link {
  font-size: 14px;
  color: #09090b;
  text-decoration: none;
  font-weight: 500;
  transition: color 0.2s;
}

.register-link:hover {
  color: #52525b;
  text-decoration: underline;
}
</style>
