import { createRouter, createWebHistory } from 'vue-router'
import LoginView from '../views/LoginView.vue'
import { useUserStore } from '../stores/useUserStore'

// Element Plus Icons
import {
  Monitor, Files, Collection, DataAnalysis,
  Timer, Setting, Odometer, Box
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
    component: LoginView
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('../views/login/Register.vue')
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
            // TODO: 替换为真实的 Dashboard 组件
            component: { template: '<div style="padding:40px;color:#999;font-size:20px;">🏠 运行大盘 - 开发中...</div>' }
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
            meta: { title: '设备管理中心' },
            component: () => import('../views/devices/DeviceCenter.vue')
          },
          {
            path: 'variables',
            name: 'global-variables',
            meta: { title: '全局变量库' },
            component: () => import('../views/variables/VariableLibrary.vue')
          },
          {
            path: 'packages',
            name: 'app-packages',
            meta: { title: 'App包管理' },
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
            meta: { title: '用例管理' },
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
            meta: { title: '场景编排' },
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
            meta: { title: '智能稳定性' },
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
            meta: { title: '流畅度分析' },
            component: () => import('@/views/special/Fluency.vue')
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
            meta: { title: '定时任务' },
            component: () => import('../views/tasks/TaskList.vue')
          },
          {
            path: 'reports',
            name: 'report-list',
            meta: { title: '报告中心' },
            component: () => import('../views/reports/ReportList.vue')
          },
          {
            path: 'reports/:id',
            name: 'report-detail',
            meta: { title: '报告详情', hidden: true },
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
            path: 'notifications',
            name: 'notification-settings',
            meta: { title: '通知设置' },
            component: () => import('../views/settings/NotificationSettings.vue')
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
          next()
        } catch (e) {
          userStore.logout()
          next('/login')
        }
      } else {
        next()
      }
    } else {
      next('/login')
    }
  }
})

export default router
