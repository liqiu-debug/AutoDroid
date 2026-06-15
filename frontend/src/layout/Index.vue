<script setup>
import { computed } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import Navbar from './components/Navbar.vue'
import ClientModeSwitch from '@/components/ClientModeSwitch.vue'
import MobileUnavailable from '@/components/MobileUnavailable.vue'
import { useClientMode } from '@/composables/useClientMode'
import { useUserStore } from '@/stores/useUserStore'
import { Collection, DataAnalysis, Files, Monitor, Odometer } from '@element-plus/icons-vue'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const canShowRoute = (routeRecord) => {
  return !routeRecord.meta?.requiresAdmin || userStore.isAdmin
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

/**
 * 判断一级菜单是否应该直接显示为 menu-item（仅有一个可见子路由时）
 */
const getVisibleChildren = (route) => {
  if (!route.children) return []
  return route.children.filter(child => !child.meta?.hidden && canShowRoute(child))
}

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
  <el-container v-if="!isMobileMode" class="layout-container" direction="vertical">
    <!-- 顶部导航栏 -->
    <el-header class="layout-header">
      <Navbar />
    </el-header>

    <el-container class="layout-body">
      <!-- 左侧动态菜单 -->
      <el-aside width="170px" class="layout-sidebar">
        <el-menu
          :default-active="route.path"
          class="sidebar-menu"
          :collapse="false"
          router
          :unique-opened="false"
          background-color="#303133"
          text-color="#cfcfcf"
          active-text-color="#409eff"
        >
          <template v-for="menu in menuRoutes" :key="menu.path">
            <!--
              情况1：没有 children 或只有一个可见 child → 直接渲染 el-menu-item
              (如"运行大盘"只有一个子路由)
            -->
            <el-menu-item
              v-if="!menu.children || getVisibleChildren(menu).length <= 1"
              :index="menu.children && getVisibleChildren(menu).length === 1
                ? '/' + menu.path + '/' + getVisibleChildren(menu)[0].path
                : '/' + menu.path"
            >
              <el-icon>
                <component :is="menu.meta.icon" />
              </el-icon>
              <template #title>{{ menu.meta.title }}</template>
            </el-menu-item>

            <!--
              情况2：有多个可见 children → 渲染 el-sub-menu + 子 el-menu-item
            -->
            <el-sub-menu v-else :index="'/' + menu.path">
              <template #title>
                <el-icon>
                  <component :is="menu.meta.icon" />
                </el-icon>
                <span>{{ menu.meta.title }}</span>
              </template>
              <el-menu-item
                v-for="child in getVisibleChildren(menu)"
                :key="child.path"
                :index="'/' + menu.path + '/' + child.path"
              >
                {{ child.meta?.title }}
              </el-menu-item>
            </el-sub-menu>
          </template>
        </el-menu>
      </el-aside>

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
        <el-button link type="danger" @click="handleMobileLogout">退出</el-button>
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
.layout-container {
  height: 100vh;
  width: 100vw;
  overflow: hidden;
  background-color: #f2f3f5;
}

.layout-header {
  padding: 0;
  height: 50px;
}

.layout-body {
  flex: 1;
  overflow: hidden;
  min-height: 0;
  min-width: 0;
}

.layout-sidebar {
  background-color: #303133;
  border-right: none;
  overflow-y: auto;
  overflow-x: hidden;
}

.sidebar-menu {
  height: 100%;
  border-right: none;
}

.layout-main {
  padding: 0;
  overflow: hidden;
  height: 100%;
  min-height: 0;
  min-width: 0;
  position: relative;
  display: flex;
  flex-direction: column;
}

/* 展开状态的一级菜单标题加深背景 */
.sidebar-menu :deep(.el-sub-menu.is-opened > .el-sub-menu__title) {
  background-color: #1a1a1d !important;
}

.mobile-layout {
  width: 100%;
  max-width: 100vw;
  height: 100dvh;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: #f6f7f9;
}

.mobile-header {
  height: 54px;
  width: 100%;
  max-width: 100vw;
  padding: 0 12px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid #e4e7ed;
  background: #ffffff;
  box-sizing: border-box;
}

.mobile-header-main {
  min-width: 0;
}

.mobile-brand {
  font-size: 12px;
  color: #909399;
  line-height: 1.2;
}

.mobile-title {
  margin-top: 2px;
  font-size: 17px;
  font-weight: 700;
  color: #303133;
  line-height: 1.2;
  max-width: min(46vw, 190px);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mobile-header-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.mobile-main {
  flex: 1;
  width: 100%;
  max-width: 100%;
  min-height: 0;
  overflow: hidden;
}

.mobile-main > * {
  min-width: 0;
  max-width: 100%;
}

.mobile-tabbar {
  width: 100%;
  max-width: 100vw;
  height: calc(58px + env(safe-area-inset-bottom, 0px));
  padding: 6px 8px calc(6px + env(safe-area-inset-bottom, 0px));
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 4px;
  flex-shrink: 0;
  border-top: 1px solid #e4e7ed;
  background: #ffffff;
  box-sizing: border-box;
}

.mobile-tabbar-item {
  border: none;
  background: transparent;
  color: #909399;
  font: inherit;
  padding: 2px 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  border-radius: 6px;
}

.mobile-tabbar-item .el-icon {
  font-size: 19px;
}

.mobile-tabbar-item span {
  font-size: 11px;
  line-height: 1.1;
}

.mobile-tabbar-item.active {
  color: #409eff;
  background: #ecf5ff;
}
</style>
