import { createRouter, createWebHistory } from 'vue-router'
import LoginView from '../views/LoginView.vue'
import { useUserStore } from '../stores/useUserStore'

// Element Plus Icons
import {
  Monitor, Files, Collection, DataAnalysis,
  Timer, Setting, Odometer, Box, UserFilled
} from '@element-plus/icons-vue'
import { lazyRoute, setLazyRouteErrorHandler, isModuleFetchFailure } from '@/utils/lazyRoute'
import { ElMessage } from 'element-plus'

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
    component: lazyRoute(() => import('../views/login/Register.vue')),
    meta: { mobileAvailable: true, mobileTitle: '注册' }
  },

  // ========== 主布局 ==========
  {
    path: '/',
    component: lazyRoute(() => import('@/layout/Index.vue')),
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
            component: lazyRoute(() => import('../views/dashboard/DashboardView.vue'))
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
            component: lazyRoute(() => import('../views/devices/DeviceCenter.vue'))
          },
          {
            path: 'variables',
            name: 'global-variables',
            meta: { title: '全局变量库', keepAlive: true },
            component: lazyRoute(() => import('../views/variables/VariableLibrary.vue'))
          },
          {
            path: 'packages',
            name: 'app-packages',
            meta: { title: 'App包管理', keepAlive: true },
            component: lazyRoute(() => import('../views/packages/PackageManagement.vue'))
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
            component: lazyRoute(() => import('../views/cases/CaseList.vue'))
          },
          {
            path: 'cases/create',
            name: 'case-create',
            meta: { title: '新建用例', hidden: true },
            component: lazyRoute(() => import('../views/cases/CaseEditor.vue'))
          },
          {
            path: 'cases/:id/edit',
            name: 'case-edit',
            meta: { title: '编辑用例', hidden: true },
            component: lazyRoute(() => import('../views/cases/CaseEditor.vue'))
          },
          {
            path: 'scenarios',
            name: 'scenario-list',
            meta: { title: '场景编排', keepAlive: true, mobileAvailable: true, mobileTitle: '场景执行' },
            component: lazyRoute(() => import('../views/scenarios/ScenarioList.vue'))
          },
          {
            path: 'scenarios/create',
            name: 'scenario-create',
            meta: { title: '新建场景', hidden: true },
            component: lazyRoute(() => import('../views/scenarios/ScenarioEditor.vue'))
          },
          {
            path: 'scenarios/:id/edit',
            name: 'scenario-edit',
            meta: { title: '编辑场景', hidden: true },
            component: lazyRoute(() => import('../views/scenarios/ScenarioEditor.vue'))
          }
        ]
      },

      // ────── 接口自动化 ──────
      {
        path: 'api-testing',
        meta: { title: '接口自动化', icon: Collection },
        redirect: '/api-testing/interfaces',
        children: [
          { path: 'interfaces', name: 'api-interface-list', meta: { title: '接口管理' }, component: lazyRoute(() => import('../views/api-testing/AssetList.vue')) },
          { path: 'interfaces/create', name: 'api-interface-create', meta: { title: '新建接口', hidden: true }, component: lazyRoute(() => import('../views/api-testing/InterfaceEditor.vue')) },
          { path: 'interfaces/:id/edit', name: 'api-interface-edit', meta: { title: '编辑接口', hidden: true }, component: lazyRoute(() => import('../views/api-testing/InterfaceEditor.vue')) },
          { path: 'scenarios', name: 'api-scenario-list', meta: { title: '自动化场景' }, component: lazyRoute(() => import('../views/api-testing/AssetList.vue')) },
          { path: 'scenarios/create', name: 'api-scenario-create', meta: { title: '新建接口场景', hidden: true }, component: lazyRoute(() => import('../views/api-testing/ScenarioEditor.vue')) },
          { path: 'scenarios/:id/edit', name: 'api-scenario-edit', meta: { title: '编辑接口场景', hidden: true }, component: lazyRoute(() => import('../views/api-testing/ScenarioEditor.vue')) },
        ],
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
            component: lazyRoute(() => import('@/views/special/Fastbot.vue'))
          },
          {
            path: 'inspection',
            name: 'ModelInspection',
            meta: {
              title: '模型化智能巡检',
              keepAlive: true,
              featureFlag: 'model_inspection'
            },
            component: lazyRoute(() => import('@/views/special/Inspection.vue'))
          },
          {
            path: 'fastbot/report/:id',
            name: 'fastbot-report',
            meta: { title: 'Fastbot 报告', hidden: true },
            component: lazyRoute(() => import('../views/fastbot/FastbotReportDetail.vue'))
          },
          {
            path: 'fluency',
            name: 'FluencyAnalysis',
            meta: { title: '流畅度分析', keepAlive: true },
            component: lazyRoute(() => import('@/views/special/Fluency.vue'))
          },
          {
            path: 'startup',
            name: 'StartupAnalysis',
            meta: { title: '冷热启动', keepAlive: true },
            component: lazyRoute(() => import('@/views/special/Startup.vue'))
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
                component: lazyRoute(() => import('@/views/special/Compatibility.vue'))
              },
              {
                path: 'page-sets',
                name: 'CompatibilityPageSets',
                meta: { title: '页面合集', keepAlive: true },
                component: lazyRoute(() => import('@/views/special/CompatibilityPageSets.vue'))
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
            component: lazyRoute(() => import('../views/tasks/TaskList.vue'))
          },
          {
            path: 'reports',
            name: 'report-list',
            meta: { title: '报告中心', keepAlive: true, mobileAvailable: true, mobileTitle: '报告' },
            component: lazyRoute(() => import('../views/reports/ReportList.vue'))
          },
          {
            path: 'reports/compatibility/:id',
            name: 'compatibility-report-detail',
            meta: { title: '兼容性报告详情', hidden: true },
            component: lazyRoute(() => import('../views/reports/CompatibilityReportDetail.vue'))
          },
          {
            path: 'reports/api/:id', name: 'api-report-detail',
            meta: { title: '接口测试报告', hidden: true },
            component: lazyRoute(() => import('../views/api-testing/RunDetail.vue')),
          },
          {
            path: 'reports/inspection/:id',
            name: 'inspection-report-detail',
            meta: {
              title: '智能巡检报告',
              hidden: true,
              mobileAvailable: true,
              mobileTitle: '巡检报告',
              featureFlag: 'model_inspection'
            },
            component: lazyRoute(() => import('../views/reports/InspectionReportDetail.vue'))
          },
          {
            path: 'reports/:id',
            name: 'report-detail',
            meta: { title: '报告详情', hidden: true, mobileAvailable: true, mobileTitle: '报告详情' },
            component: lazyRoute(() => import('../views/reports/ReportDetail.vue'))
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
            component: lazyRoute(() => import('../views/admin/UserManagement.vue'))
          },
          {
            path: 'notifications',
            name: 'notification-settings',
            meta: { title: '系统设置', keepAlive: true },
            component: lazyRoute(() => import('../views/settings/NotificationSettings.vue'))
          },
          {
            path: 'tokens',
            name: 'api-tokens',
            meta: { title: 'API Token', keepAlive: true },
            component: lazyRoute(() => import('../views/settings/ApiTokens.vue'))
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
            component: lazyRoute(() => import('../views/account/ChangePassword.vue'))
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
  const requiredFeature = to.matched
    .map(record => record.meta?.featureFlag)
    .find(Boolean)

  if (token && !userStore.token) {
    userStore.token = token
  }
  if (!token && userStore.token) {
    // 双向同步：localStorage 已空（401 处理/其他标签页登出）时清 pinia，
    // 消除「pinia 残留 + localStorage 空」的半迁移态（此时任何请求都会被
    // 发起前门闩静默拒绝，页面按未登录收口到 /login）。
    userStore.logout()
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
          // silent401：守卫取 userInfo 的 401 不弹「会话过期」框（弹框只服务
          // 会话中途的后台 401），由守卫静默收口 /login。
          await userStore.fetchUserInfo({ silent401: true })
          if ((requiresAdmin && !userStore.isAdmin)
            || (requiredFeature && !userStore.featureFlags?.[requiredFeature])) {
            next('/')
          } else {
            next()
          }
        } catch (e) {
          if (e?.authFailure) {
            // 鉴权失败（401/会话门闩）：fetchUserInfo 已被动清态，静默收口登录页
            userStore.logout()
            next('/login')
          } else {
            // 非鉴权错误（网络抖动/超时）：不销毁会话、不强制跳登录，普通路由
            // 放行进入（页面级请求会自报错），下次导航再惰性重试 userInfo。
            // 但 requiresAdmin / featureFlag 门禁要靠 userInfo 才能判定——
            // 判不了就不能放行（否则网络抖动时任何人都能进管理员/灰度路由），
            // 一律收口回首页。
            if (requiresAdmin || requiredFeature) {
              next('/')
            } else {
              next()
            }
          }
        }
      } else {
        if ((requiresAdmin && !userStore.isAdmin)
          || (requiredFeature && !userStore.featureFlags?.[requiredFeature])) {
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

// 懒加载失败可见化：路由 chunk 拉取失败不再「点击无反应」——
// 包装器负责重试/自动重载；此处兜底漏网的加载失败并给出提示。
setLazyRouteErrorHandler((message) => ElMessage.error(message))
router.onError((error, to) => {
  if (isModuleFetchFailure(error)) {
    ElMessage.error('页面加载失败，请刷新后重试' + (to && to.fullPath ? `（${to.fullPath}）` : ''))
  } else if (typeof console !== 'undefined') {
    console.warn('router error:', error)
  }
})

export default router
