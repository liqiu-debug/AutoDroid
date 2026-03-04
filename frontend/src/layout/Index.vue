<script setup>
import { computed } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import Navbar from './components/Navbar.vue'

const route = useRoute()
const router = useRouter()

/**
 * 从路由配置中提取菜单路由
 * 仅取 Layout 下的 children，过滤掉 meta.hidden 的路由
 */
const menuRoutes = computed(() => {
  const layoutRoute = router.options.routes.find(r => r.path === '/')
  if (!layoutRoute || !layoutRoute.children) return []
  return layoutRoute.children.filter(r => r.meta && !r.meta.hidden)
})

/**
 * 判断一级菜单是否应该直接显示为 menu-item（仅有一个可见子路由时）
 */
const getVisibleChildren = (route) => {
  if (!route.children) return []
  return route.children.filter(child => !child.meta?.hidden)
}
</script>

<template>
  <el-container class="layout-container" direction="vertical">
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
        <RouterView />
      </el-main>
    </el-container>
  </el-container>
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
  position: relative;
  display: flex;
  flex-direction: column;
}

/* 展开状态的一级菜单标题加深背景 */
.sidebar-menu :deep(.el-sub-menu.is-opened > .el-sub-menu__title) {
  background-color: #1a1a1d !important;
}
</style>
