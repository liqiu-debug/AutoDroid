<script setup>
import { useUserStore } from '@/stores/useUserStore'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { SwitchButton } from '@element-plus/icons-vue'

const userStore = useUserStore()
const router = useRouter()

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
      <span class="user-name">Hi, {{ userStore.userInfo?.full_name || userStore.userInfo?.username }}</span>
      <el-button type="danger" link :icon="SwitchButton" @click="handleLogout" class="logout-btn">
        退出
      </el-button>
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
}

.user-name {
  font-size: 14px;
  margin-right: 15px;
  font-weight: 500;
  color: #E6E8EB;
}

.logout-btn {
  font-size: 16px;
  color: #F56C6C;
}
.logout-btn:hover {
  background-color: transparent;
  opacity: 0.8;
}
</style>
