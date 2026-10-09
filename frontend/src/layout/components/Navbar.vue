<script setup>
import { computed } from 'vue'
import { useUserStore } from '@/stores/useUserStore'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { ArrowDown, Lock, SwitchButton } from '@element-plus/icons-vue'

const userStore = useUserStore()
const router = useRouter()
const userName = computed(() => userStore.userInfo?.full_name || userStore.userInfo?.username || '账户')
const handleCommand = async command => {
  if (command === 'password') return router.push('/account/password')
  if (command !== 'logout') return
  try {
    await ElMessageBox.confirm('确定要退出登录吗？', '退出登录', { confirmButtonText: '退出', cancelButtonText: '取消' })
    userStore.logout()
    router.push('/login')
  } catch { /* User kept the current session. */ }
}
</script>
<template>
  <el-dropdown class="account-menu" trigger="click" @command="handleCommand">
    <button class="user-trigger" type="button" :aria-label="`${userName}的账户菜单`">
      <span class="avatar" aria-hidden="true">{{ userName.slice(0, 1).toUpperCase() }}</span>
      <span class="user-name">{{ userName }}</span>
      <el-icon><ArrowDown /></el-icon>
    </button>
    <template #dropdown>
      <el-dropdown-menu>
        <el-dropdown-item command="password" :icon="Lock">修改密码</el-dropdown-item>
        <el-dropdown-item command="logout" :icon="SwitchButton" divided>退出登录</el-dropdown-item>
      </el-dropdown-menu>
    </template>
  </el-dropdown>
</template>
<style scoped>
.account-menu { flex-shrink: 0; min-width: 0; }
.user-trigger { min-height: 30px; max-width: 190px; display: flex; align-items: center; gap: 8px; padding: 2px 8px 2px 2px; border: 0; border-radius: 6px; background: transparent; color: var(--ad-text); font: inherit; cursor: pointer; }
.user-trigger:hover { background: var(--ad-primary-soft); }
.avatar { width: 24px; height: 24px; flex-shrink: 0; display: grid; place-items: center; border-radius: 50%; border: 1px solid var(--ad-border); background: var(--ad-surface); color: var(--ad-muted); font-size: 12px; }
.user-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: left; font-size: 12px; }
.user-trigger > .el-icon { font-size: 12px; color: var(--ad-muted); flex-shrink: 0; }
</style>
