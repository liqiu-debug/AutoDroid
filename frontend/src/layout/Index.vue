<script setup>
import { computed, ref, watch, nextTick } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import Navbar from './components/Navbar.vue'
import SidebarMenuItem from './components/SidebarMenuItem.vue'
import ClientModeSwitch from '@/components/ClientModeSwitch.vue'
import MobileUnavailable from '@/components/MobileUnavailable.vue'
import { useClientMode } from '@/composables/useClientMode'
import { useUserStore } from '@/stores/useUserStore'
import { Collection, DataAnalysis, Files, Monitor, Odometer, Cpu } from '@element-plus/icons-vue'

import { activeMenuPath, menuAncestors } from '@/utils/navigation'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const canShowRoute = (routeRecord) => {
  if (routeRecord.meta?.requiresAdmin && !userStore.isAdmin) return false
  const featureFlag = routeRecord.meta?.featureFlag
  return !featureFlag || userStore.featureFlags?.[featureFlag] === true
}
const { isMobileMode } = useClientMode()

/**
 * 从路由配置中提取菜单路由
 * 仅取 Layout 下的 children，过滤掉 meta.hidden 的路由
 */
const menuRoutes = computed(() => {
  const layoutRoute = router.options.routes.find(r => r.path === '/')
  if (!layoutRoute || !layoutRoute.children) return []
  return layoutRoute.children.filter(r => r.meta && !r.meta.hidden && canShowRoute(r))
})

const menuRef = ref(null)
const activeMenu = computed(() => activeMenuPath(route.path))
const openMenus = computed(() => menuAncestors(route.path))
const breadcrumbs = computed(() => route.matched.filter(item => item.meta?.title).map(item => item.meta.title))
watch(() => route.path, async () => {
  await nextTick()
  openMenus.value.forEach(path => menuRef.value?.open(path))
})

const mobileNavItems = computed(() => [
  { path: '/dashboard', label: '概览', icon: Odometer },
  { path: '/assets/devices', label: '设备', icon: Monitor },
  { path: '/ui/cases', label: '用例', icon: Files },
  { path: '/ui/scenarios', label: '场景', icon: Collection },
  { path: '/execution/reports', label: '报告', icon: DataAnalysis },
])

const mobileTitle = computed(() => route.meta?.mobileTitle || route.meta?.title || 'AutoDroid')
const isMobileRouteAllowed = computed(() => route.meta?.mobileAvailable === true)

const isMobileNavActive = (path) => {
  if (path === '/execution/reports') return route.path.startsWith('/execution/reports')
  return route.path === path
}

const handleMobileNav = (path) => {
  if (route.path !== path) router.push(path)
}

const handleMobileLogout = () => {
  ElMessageBox.confirm('确定要退出登录吗？', '提示', {
    confirmButtonText: '确定',
    cancelButtonText: '取消',
    type: 'warning',
  }).then(() => {
    userStore.logout()
    router.push('/login')
  }).catch(() => {})
}
</script>

<template>
  <el-container v-if="!isMobileMode" class="layout-container">
    <el-aside width="184px" class="layout-sidebar">
      <router-link class="sidebar-brand" to="/dashboard" aria-label="AutoDroid 运行概览">
        <el-icon><Cpu /></el-icon><span>AutoDroid</span>
      </router-link>
      <div class="sidebar-caption">自动化测试工作台</div>
      <nav class="sidebar-scroll" aria-label="主导航">
        <el-menu ref="menuRef" :default-active="activeMenu" :default-openeds="openMenus"
          class="sidebar-menu" :collapse="false" router :unique-opened="false">
          <SidebarMenuItem v-for="menu in menuRoutes" :key="menu.path" :item="menu" :can-show-route="canShowRoute" />
        </el-menu>
      </nav>
    </el-aside>
    <el-container class="layout-body" direction="vertical">
      <el-header class="layout-header">
        <el-breadcrumb separator="/" aria-label="当前位置">
          <el-breadcrumb-item v-for="(title, index) in breadcrumbs" :key="index">{{ title }}</el-breadcrumb-item>
        </el-breadcrumb>
        <div class="header-actions"><ClientModeSwitch /><Navbar /></div>
      </el-header>
      <!-- 右侧内容区 -->
      <el-main class="layout-main">
        <RouterView v-slot="{ Component, route: currentRoute }">
          <KeepAlive>
            <component
              :is="Component"
              v-if="currentRoute.meta?.keepAlive"
              :key="String(currentRoute.name || currentRoute.path)"
            />
          </KeepAlive>
          <component
            :is="Component"
            v-if="!currentRoute.meta?.keepAlive"
            :key="currentRoute.fullPath"
          />
        </RouterView>
      </el-main>
    </el-container>
  </el-container>

  <div v-else class="mobile-layout">
    <header class="mobile-header">
      <div class="mobile-header-main">
        <div class="mobile-brand">AutoDroid</div>
        <div class="mobile-title">{{ mobileTitle }}</div>
      </div>
      <div class="mobile-header-actions">
        <ClientModeSwitch compact />
        <el-button link @click="handleMobileLogout">退出</el-button>
      </div>
    </header>

    <main class="mobile-main">
      <MobileUnavailable v-if="!isMobileRouteAllowed" />
      <RouterView v-else v-slot="{ Component, route: currentRoute }">
        <KeepAlive>
          <component
            :is="Component"
            v-if="currentRoute.meta?.keepAlive"
            :key="String(currentRoute.name || currentRoute.path)"
          />
        </KeepAlive>
        <component
          :is="Component"
          v-if="!currentRoute.meta?.keepAlive"
          :key="currentRoute.fullPath"
        />
      </RouterView>
    </main>

    <nav class="mobile-tabbar">
      <button
        v-for="item in mobileNavItems"
        :key="item.path"
        class="mobile-tabbar-item"
        :aria-current="isMobileNavActive(item.path) ? 'page' : undefined"
        :class="{ active: isMobileNavActive(item.path) }"
        type="button"
        @click="handleMobileNav(item.path)"
      >
        <el-icon><component :is="item.icon" /></el-icon>
        <span>{{ item.label }}</span>
      </button>
    </nav>
  </div>
</template>

<style scoped>
.layout-container { height: 100dvh; width: 100%; overflow: hidden; background: var(--ad-bg); }
.layout-sidebar { width: 184px; background: var(--ad-sidebar); border-right: 1px solid var(--ad-border); display: flex; flex-direction: column; overflow: hidden; }
.sidebar-brand { display: flex; align-items: center; gap: 9px; padding: 22px 20px 12px; text-decoration: none; color: var(--ad-text); font-size: 17px; font-weight: 600; letter-spacing: -.4px; }
.sidebar-brand .el-icon { font-size: 23px; color: var(--ad-primary); }
.sidebar-caption { margin: 0 20px 20px; font-size: 12px; color: var(--ad-muted); }
.sidebar-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 0 8px 12px; }
.sidebar-menu { border-right: 0; background: transparent; --el-menu-bg-color: transparent; --el-menu-text-color: var(--ad-muted); --el-menu-active-color: var(--ad-primary); --el-menu-hover-bg-color: var(--ad-primary-soft); --el-menu-item-height: 36px; --el-menu-sub-item-height: 32px; --el-menu-base-level-padding: 12px; --el-menu-level-padding: 12px; }
.sidebar-menu :deep(.el-menu-item), .sidebar-menu :deep(.el-sub-menu__title) { font-size: 13px; border-radius: 6px; margin-bottom: 3px; height: 36px; line-height: 36px; padding-right: 24px; }
.sidebar-menu :deep(.el-sub-menu .el-menu-item) { font-size: 13px; height: 32px; line-height: 32px; min-width: 0; }
.sidebar-menu :deep(.el-menu-item.is-active) { background: var(--ad-primary-soft); font-weight: 500; }
.sidebar-menu :deep(.el-sub-menu.is-active > .el-sub-menu__title) { color: var(--ad-text); font-weight: 500; }
.sidebar-menu :deep(.el-icon) { font-size: 16px; width: 18px; margin-right: 8px; }
.sidebar-menu :deep(.el-sub-menu__icon-arrow) { width: 12px; margin: 0; font-size: 11px; right: 10px; }
.layout-body { flex: 1; overflow: hidden; min-width: 0; min-height: 0; }
.layout-header { height: 44px; flex-shrink: 0; padding: 0 16px; display: flex; justify-content: space-between; align-items: center; gap: 12px; background: var(--ad-surface); border-bottom: 1px solid var(--ad-border); }
.header-actions { display: flex; align-items: center; gap: 10px; flex-shrink: 0; min-width: 0; }
.layout-header :deep(.el-breadcrumb) { font-size: 12px; min-width: 0; }
.layout-header :deep(.el-breadcrumb__inner) { color: var(--ad-muted); font-weight: 400; }
.layout-main { padding: 0; overflow: hidden; min-height: 0; min-width: 0; position: relative; display: flex; flex-direction: column; }
.mobile-layout { width: 100%; height: 100dvh; display: flex; flex-direction: column; overflow: hidden; background: var(--ad-bg); }
.mobile-header { padding: 4px 12px; min-height: 60px; flex-shrink: 0; display: flex; align-items: center; justify-content: space-between; gap: 8px; border-bottom: 1px solid var(--ad-border); background: var(--ad-surface); }
.mobile-header-main { min-width: 0; }
.mobile-brand { font-size: 14px; color: var(--ad-muted); line-height: 1.2; }
.mobile-title { margin-top: 3px; font-size: 17px; font-weight: 600; color: var(--ad-text); max-width: min(48vw, 240px); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mobile-header-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.mobile-main { flex: 1; width: 100%; min-height: 0; overflow: hidden; }
.mobile-main > * { min-width: 0; max-width: 100%; }
.mobile-tabbar { width: 100%; min-height: calc(62px + env(safe-area-inset-bottom, 0px)); padding: 6px 8px calc(6px + env(safe-area-inset-bottom, 0px)); display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 4px; flex-shrink: 0; border-top: 1px solid var(--ad-border); background: var(--ad-surface); }
.mobile-tabbar-item { border: none; background: transparent; color: var(--ad-muted); font: inherit; padding: 3px 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px; border-radius: 6px; min-height: 44px; }
.mobile-tabbar-item .el-icon { font-size: 19px; }
.mobile-tabbar-item span { font-size: 14px; line-height: 1.3; }
.mobile-tabbar-item.active { color: var(--ad-primary); background: var(--ad-primary-soft); }
</style>
