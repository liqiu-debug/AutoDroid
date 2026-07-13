import { createRouter, createWebHistory } from 'vue-router'
import LoginView from '../views/LoginView.vue'
import { useUserStore } from '../stores/useUserStore'

// Element Plus Icons
import {
  Monitor, Files, Collection, DataAnalysis,
  Timer, Setting, Odometer, Box, UserFilled
} from '@element-plus/icons-vue'

/**
 * 路由配置
 * - meta.title: 菜单显示文字
 * - meta.icon:  菜单图标组件
 * - meta.hidden: true 时不在侧边栏显示
 */
const routes = [
  // ========== 认证页面（Layout 外部）==========
  {
    path: '/login',
    name: 'login',
    component: LoginView,
    meta: { mobileAvailable: true, mobileTitle: '登录' }
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('../views/login/Register.vue'),
    meta: { mobileAvailable: true, mobileTitle: '注册' }
  },

  // ========== 主布局 ==========
  {
    path: '/',
    component: () => import('@/layout/Index.vue'),
    redirect: '/dashboard',
    children: [
      // ────── 🏠 运行大盘 ──────
      {
        path: 'dashboard',
        meta: { title: '运行大盘', icon: Odometer },
        children: [
          {
            path: '',
            name: 'dashboard',
            meta: { keepAlive: true, mobileAvailable: true, mobileTitle: '运行概览' },
            component: () => import('../views/dashboard/DashboardView.vue')
          }
        ]
      },

      // ────── 📱 测试资产 ──────
      {
        path: 'assets',
        meta: { title: '测试资产', icon: Box },
        redirect: '/assets/devices',
        children: [
          {
            path: 'devices',
            name: 'device-center',
            meta: { title: '设备管理中心', keepAlive: true, mobileAvailable: true, mobileTitle: '设备状态' },
            component: () => import('../views/devices/DeviceCenter.vue')
          },
          {
            path: 'variables',
            name: 'global-variables',
            meta: { title: '全局变量库', keepAlive: true },
            component: () => import('../views/variables/VariableLibrary.vue')
          },
          {
            path: 'packages',
            name: 'app-packages',
            meta: { title: 'App包管理', keepAlive: true },
            component: () => import('../views/packages/PackageManagement.vue')
          }
        ]
      },

      // ────── 🎬 UI 自动化 ──────
      {
        path: 'ui',
        meta: { title: 'UI 自动化', icon: Files },
        redirect: '/ui/cases',
        children: [
          {
            path: 'cases',
            name: 'case-list',
            meta: { title: '用例管理', keepAlive: true, mobileAvailable: true, mobileTitle: '用例执行' },
            component: () => import('../views/cases/CaseList.vue')
          },
          {
            path: 'cases/create',
            name: 'case-create',
            meta: { title: '新建用例', hidden: true },
            component: () => import('../views/cases/CaseEditor.vue')
          },
          {
            path: 'cases/:id/edit',
            name: 'case-edit',
            meta: { title: '编辑用例', hidden: true },
            component: () => import('../views/cases/CaseEditor.vue')
          },
          {
            path: 'scenarios',
            name: 'scenario-list',
            meta: { title: '场景编排', keepAlive: true, mobileAvailable: true, mobileTitle: '场景执行' },
            component: () => import('../views/scenarios/ScenarioList.vue')
          },
          {
            path: 'scenarios/create',
            name: 'scenario-create',
            meta: { title: '新建场景', hidden: true },
            component: () => import('../views/scenarios/ScenarioEditor.vue')
          },
          {
            path: 'scenarios/:id/edit',
            name: 'scenario-edit',
            meta: { title: '编辑场景', hidden: true },
            component: () => import('../views/scenarios/ScenarioEditor.vue')
          }
        ]
      },

      // ────── 🧪 专项测试 ──────
      {
        path: 'special',
        name: 'SpecializedTest',
        meta: { title: '专项测试', icon: Odometer },
        redirect: '/special/fastbot',
        children: [
          {
            path: 'fastbot',
            name: 'FastbotStability',
            meta: { title: '智能稳定性', keepAlive: true },
            component: () => import('@/views/special/Fastbot.vue')
          },
          {
            path: 'fastbot/report/:id',
            name: 'fastbot-report',
            meta: { title: 'Fastbot 报告', hidden: true },
            component: () => import('../views/fastbot/FastbotReportDetail.vue')
          },
          {
            path: 'fluency',
            name: 'FluencyAnalysis',
            meta: { title: '流畅度分析', keepAlive: true },
            component: () => import('@/views/special/Fluency.vue')
          },
          {
            path: 'startup',
            name: 'StartupAnalysis',
            meta: { title: '冷热启动', keepAlive: true },
            component: () => import('@/views/special/Startup.vue')
          },
          {
            path: 'compatibility',
            name: 'CompatibilityTest',
            meta: { title: '兼容性测试', alwaysShow: true },
            redirect: '/special/compatibility/run',
            children: [
              {
                path: 'run',
                name: 'CompatibilityRun',
                meta: { title: '测试配置', keepAlive: true },
                component: () => import('@/views/special/Compatibility.vue')
              },
              {
                path: 'page-sets',
                name: 'CompatibilityPageSets',
                meta: { title: '页面合集', keepAlive: true },
                component: () => import('@/views/special/CompatibilityPageSets.vue')
              }
            ]
          }
        ]
      },

      // ────── ⚡ 调度与分析 ──────
      {
        path: 'execution',
        meta: { title: '调度与分析', icon: DataAnalysis },
        redirect: '/execution/tasks',
        children: [
          {
            path: 'tasks',
            name: 'task-list',
            meta: { title: '定时任务', keepAlive: true },
            component: () => import('../views/tasks/TaskList.vue')
          },
          {
            path: 'reports',
            name: 'report-list',
            meta: { title: '报告中心', keepAlive: true, mobileAvailable: true, mobileTitle: '报告' },
            component: () => import('../views/reports/ReportList.vue')
          },
          {
            path: 'reports/compatibility/:id',
            name: 'compatibility-report-detail',
            meta: { title: '兼容性报告详情', hidden: true },
            component: () => import('../views/reports/CompatibilityReportDetail.vue')
          },
          {
            path: 'reports/:id',
            name: 'report-detail',
            meta: { title: '报告详情', hidden: true, mobileAvailable: true, mobileTitle: '报告详情' },
            component: () => import('../views/reports/ReportDetail.vue')
          }
        ]
      },

      // ────── ⚙️ 系统配置 ──────
      {
        path: 'settings',
        meta: { title: '系统配置', icon: Setting },
        children: [
          {
            path: 'users',
            name: 'admin-users',
            meta: { title: '用户管理', keepAlive: true, requiresAdmin: true },
            component: () => import('../views/admin/UserManagement.vue')
          },
          {
            path: 'notifications',
            name: 'notification-settings',
            meta: { title: '通知设置', keepAlive: true },
            component: () => import('../views/settings/NotificationSettings.vue')
          },
          {
            path: 'tokens',
            name: 'api-tokens',
            meta: { title: 'API Token', keepAlive: true },
            component: () => import('../views/settings/ApiTokens.vue')
          }
        ]
      },

      // ────── 账号设置 ──────
      {
        path: 'account',
        meta: { title: '账号设置', icon: UserFilled, hidden: true },
        children: [
          {
            path: 'password',
            name: 'account-password',
            meta: { title: '修改密码', hidden: true },
            component: () => import('../views/account/ChangePassword.vue')
          }
        ]
      }
    ]
  }
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes
})

router.beforeEach(async (to, from, next) => {
  const userStore = useUserStore()
  const token = localStorage.getItem('token')
  const requiresAdmin = to.matched.some(record => record.meta?.requiresAdmin)

  if (token && !userStore.token) {
    userStore.token = token
  }

  if (to.path === '/login' || to.path === '/register') {
    if (userStore.token) {
      next('/')
    } else {
      next()
    }
  } else {
    if (userStore.token) {
      if (!userStore.userInfo) {
        try {
          await userStore.fetchUserInfo()
          if (requiresAdmin && !userStore.isAdmin) {
            next('/')
          } else {
            next()
          }
        } catch (e) {
          userStore.logout()
          next('/login')
        }
      } else {
        if (requiresAdmin && !userStore.isAdmin) {
          next('/')
        } else {
          next()
        }
      }
    } else {
      next('/login')
    }
  }
})

export default router
