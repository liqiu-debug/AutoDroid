<script setup>
import { useUserStore } from '@/stores/useUserStore'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { ArrowDown, Lock, SwitchButton } from '@element-plus/icons-vue'
import ClientModeSwitch from '@/components/ClientModeSwitch.vue'

const userStore = useUserStore()
const router = useRouter()

const handleCommand = (command) => {
  if (command === 'password') {
    router.push('/account/password')
  }
  if (command === 'logout') {
    handleLogout()
  }
}

const handleLogout = () => {
  ElMessageBox.confirm('确定要退出登录吗？', '提示', {
    confirmButtonText: '确定',
    cancelButtonText: '取消',
    type: 'warning',
  }).then(() => {
    userStore.logout()
    router.push('/login')
  })
}
</script>

<template>
  <div class="navbar">
    <div class="left">
      <div class="brand">AutoDroid</div>
    </div>
    <div class="right-menu">
      <ClientModeSwitch light />
      <el-dropdown trigger="click" @command="handleCommand">
        <button class="user-trigger" type="button">
          <span class="user-name">Hi, {{ userStore.userInfo?.full_name || userStore.userInfo?.username }}</span>
          <el-icon class="arrow-icon"><ArrowDown /></el-icon>
        </button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="password">
              <el-icon><Lock /></el-icon>
              修改密码
            </el-dropdown-item>
            <el-dropdown-item command="logout" divided>
              <el-icon><SwitchButton /></el-icon>
              退出登录
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>
  </div>
</template>

<style scoped>
.navbar {
  height: 100%;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 20px;
  background-color: #303133;
  color: #fff;
  border-bottom: 1px solid #484a4d;
}

.brand {
  font-size: 18px;
  font-weight: bold;
  letter-spacing: 1px;
}

.right-menu {
  display: flex;
  align-items: center;
  gap: 12px;
}

.user-trigger {
  height: 34px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0 10px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: #E6E8EB;
  cursor: pointer;
  font: inherit;
}

.user-trigger:hover {
  background-color: rgba(255, 255, 255, 0.08);
}

.user-name {
  font-size: 14px;
  font-weight: 500;
}

.arrow-icon {
  font-size: 12px;
}

</style>
