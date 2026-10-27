import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { ref, computed } from 'vue'

import {
  createSessionExpiredController,
  hasSessionToken,
  SESSION_EXPIRED_MESSAGE,
  TOKEN_STORAGE_KEY,
} from '../src/utils/sessionExpiry.js'

// 鉴权链契约测试（B 组修复）：
// - 401 单飞：只处理首个（模块级 redirecting 门闩），处理器可注入；
//   未登录（localStorage 无 token）时不弹框、不跳转、不清理；
// - 默认流程：useUserStore.logout() 清态 → ElMessageBox.confirm
//   「登录状态已过期，请重新登录」→ 用户点击后 router.push('/login')（用户手势
//   内跳转，绕开桌面壳吞程序化跳转）；取消则留在当前页；
// - 请求门闩：无 token 的非预登录请求「发起前」本地拒绝（401 后轮询自动静默停止）；
// - F4：fetchUserInfo 仅鉴权失败（401/会话门闩）logout 并标记 error.authFailure；
//   非鉴权错误（网络抖动/超时）保会话 rethrow（authFailure=false），守卫放行进入；
// - F5：守卫取 userInfo 的请求带 silent401（拦截器对该请求不弹「会话过期」框），
//   鉴权失败由守卫静默 next('/login')；弹框只服务会话中途的后台 401；
//   路由守卫 localStorage↔pinia 双向同步，DeviceCenter 轮询 KeepAlive 停表。
// 与仓库既有测试一致，用 node:test + 依赖注入执行 api/store 层（非 vitest）。

const stripImports = source => source.replace(/^import [\s\S]*? from ['"][^'"]+['"]\s*\n/gm, '')
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve() }

const fakeStorage = (token) => {
  const map = new Map(token ? [['token', token]] : [])
  return {
    getItem: key => map.get(key) ?? null,
    setItem: (key, value) => map.set(key, value),
    removeItem: key => map.delete(key),
  }
}

// ==================== sessionExpiry 控制器（纯逻辑单测） ====================

test('hasSessionToken 读注入存储；无 token / 无存储时为 false', () => {
  assert.equal(hasSessionToken(fakeStorage('tok-1')), true)
  assert.equal(hasSessionToken(fakeStorage(null)), false)
  assert.equal(hasSessionToken(null), false, '无 window.localStorage 环境安全返回 false')
})

test('默认流程：先经 logout 清态，弹「登录状态已过期，请重新登录」，确认后才跳 /login', async () => {
  const calls = []
  const confirmGate = deferred()
  const controller = createSessionExpiredController({
    storage: fakeStorage('tok-1'),
    logout: async () => calls.push(['logout']),
    confirm: (message, title, options) => { calls.push(['confirm', message, title, options]); return confirmGate.promise },
    navigate: path => calls.push(['navigate', path]),
  })

  const handling = controller.handleSessionExpired()
  await flush()
  assert.deepEqual(calls.map(c => c[0]), ['logout', 'confirm'], '先清态再弹框')
  assert.equal(calls[1][1], SESSION_EXPIRED_MESSAGE)
  assert.equal(calls[1][1], '登录状态已过期，请重新登录')
  assert.deepEqual(calls[1][3], {
    confirmButtonText: '重新登录',
    cancelButtonText: '取消',
    type: 'warning',
  })
  assert.equal(calls.some(c => c[0] === 'navigate'), false, '用户点击前不跳转')

  confirmGate.resolve('confirm')
  await handling
  assert.deepEqual(calls[calls.length - 1], ['navigate', '/login'], '用户点击后才 router.push(/login)')
})

test('取消弹框：留在当前页、不跳转、不抛错', async () => {
  const calls = []
  const controller = createSessionExpiredController({
    storage: fakeStorage('tok-1'),
    logout: async () => calls.push(['logout']),
    confirm: () => Promise.reject(new Error('cancel')),
    navigate: path => calls.push(['navigate', path]),
  })
  await controller.handleSessionExpired() // 必须 resolve 而非 reject
  assert.equal(calls.some(c => c[0] === 'navigate'), false, '取消不得跳转')
})

test('单飞：呈现未结束前的其余 401 一律静默，只处理首个', async () => {
  const calls = []
  const confirmGate = deferred()
  const controller = createSessionExpiredController({
    storage: fakeStorage('tok-1'),
    logout: async () => calls.push(['logout']),
    confirm: () => { calls.push(['confirm']); return confirmGate.promise },
    navigate: path => calls.push(['navigate', path]),
  })

  const first = controller.handleSessionExpired()
  await flush()
  assert.equal(controller.isRedirecting(), true)
  await controller.handleSessionExpired()
  await controller.handleSessionExpired()
  assert.equal(calls.filter(c => c[0] === 'confirm').length, 1, '只弹一框')
  assert.equal(calls.filter(c => c[0] === 'logout').length, 1, '只清一次态')

  confirmGate.resolve('confirm')
  await first
  assert.equal(controller.isRedirecting(), false)
})

test('未登录（无 token）：静默放行，不弹框、不清理、不跳转', async () => {
  const calls = []
  const controller = createSessionExpiredController({
    storage: fakeStorage(null),
    logout: async () => calls.push(['logout']),
    confirm: () => { calls.push(['confirm']); return Promise.resolve() },
    navigate: path => calls.push(['navigate', path]),
    handler: () => calls.push(['handler']),
  })
  await controller.handleSessionExpired()
  await controller.handleSessionExpired()
  assert.deepEqual(calls, [], '未登录时的 401 全部静默（不得弹多次框）')
})

test('可注入「会话过期」处理器：替换默认弹框流程，收到 navigate 与文案', async () => {
  const calls = []
  const confirm = () => { calls.push(['confirm']); return Promise.resolve() }
  const controller = createSessionExpiredController({
    storage: fakeStorage('tok-1'),
    logout: async () => calls.push(['logout']),
    confirm,
    navigate: path => calls.push(['navigate', path]),
    handler: async ctx => { calls.push(['handler', ctx.message, typeof ctx.navigate]) },
  })
  await controller.handleSessionExpired()
  assert.deepEqual(calls.map(c => c[0]), ['logout', 'handler'], '注入处理器接管呈现')
  assert.equal(calls[1][1], '登录状态已过期，请重新登录')
  assert.equal(calls[1][2], 'function')

  controller.setHandler(null) // 恢复默认流程
  await controller.handleSessionExpired()
  assert.equal(calls.filter(c => c[0] === 'confirm').length, 1, 'setHandler(null) 恢复默认弹框')
})

test('处理完成后重新登录再过期：可再次处理（门闩会复位）', async () => {
  const storage = fakeStorage('tok-1')
  const calls = []
  const controller = createSessionExpiredController({
    storage,
    logout: async () => storage.removeItem(TOKEN_STORAGE_KEY),
    confirm: () => { calls.push(['confirm']); return Promise.resolve() },
    navigate: path => calls.push(['navigate', path]),
  })
  await controller.handleSessionExpired()
  assert.equal(hasSessionToken(storage), false)
  await controller.handleSessionExpired() // 已登出：静默
  assert.equal(calls.filter(c => c[0] === 'confirm').length, 1)

  storage.setItem(TOKEN_STORAGE_KEY, 'tok-2') // 重新登录
  await controller.handleSessionExpired()
  assert.equal(calls.filter(c => c[0] === 'confirm').length, 2, '再次过期重新弹框')
})

// ==================== api/index.js 接线（401 单飞 + 请求门闩） ====================

function apiSetup({ token = 'tok-1' } = {}) {
  const storage = fakeStorage(token)
  const hooks = { request: null, responseFail: null }
  const instance = {
    interceptors: {
      request: { use: ok => { hooks.request = ok } },
      response: { use: (ok, fail) => { hooks.responseFail = fail } },
    },
    get: async () => ({ data: {} }),
    post: async () => ({ data: {} }),
    put: async () => ({ data: {} }),
    patch: async () => ({ data: {} }),
    delete: async () => ({ data: {} }),
  }
  const windowObj = { localStorage: storage, location: { pathname: '/dashboard', href: '/dashboard' } }
  const source = stripImports(read('../src/api/index.js'))
    .replace('export default', 'const __default =')
    .replace(/export const/g, 'const')
  const deps = {
    axios: { create: () => instance },
    normalizeError: error => error,
    versionRequestConfig: () => ({}),
    window: windowObj,
    localStorage: storage,
    sessionStorage: { setItem: () => {}, getItem: () => null, removeItem: () => {} },
    createSessionExpiredController,
    hasSessionToken,
    SESSION_EXPIRED_MESSAGE,
    TOKEN_STORAGE_KEY,
  }
  const exposed = new Function(
    ...Object.keys(deps),
    source + '\nreturn { api: __default, setSessionExpiredHandler, resetSessionExpired }',
  )(...Object.values(deps))
  return { ...exposed, hooks, storage, windowObj, source }
}

const error401 = () => ({ response: { status: 401, data: {} }, message: 'unauthorized' })

test('401 单飞：只触发首个会话过期处理，清 token，不再 window.location 硬跳', async () => {
  const context = apiSetup()
  const seen = []
  const gate = deferred()
  context.setSessionExpiredHandler(ctx => { seen.push(ctx); return gate.promise })

  context.hooks.responseFail(error401()).catch(() => {})
  context.hooks.responseFail(error401()).catch(() => {})
  context.hooks.responseFail(error401()).catch(() => {})
  await flush()

  assert.equal(seen.length, 1, '三个并发 401 只处理首个（单飞）')
  assert.equal(typeof seen[0].navigate, 'function', '处理器拿到 navigate')
  assert.equal(seen[0].message, '登录状态已过期，请重新登录')
  assert.equal(context.storage.getItem('token'), null, '经统一清理掉 localStorage token')
  assert.equal(context.windowObj.location.href, '/dashboard', '绝不 window.location.href 硬跳')

  gate.resolve()
  await flush()
  const rejected = context.hooks.responseFail(error401()).then(() => 'resolved', () => 'rejected')
  assert.equal(await rejected, 'rejected', '401 仍照常 reject 给调用方')
})

test('401 但未登录：处理器不触发（不得弹多次框）', async () => {
  const context = apiSetup({ token: null })
  const seen = []
  context.setSessionExpiredHandler(() => seen.push(1))
  context.hooks.responseFail(error401()).catch(() => {})
  context.hooks.responseFail(error401()).catch(() => {})
  await flush()
  assert.deepEqual(seen, [])
})

test('请求门闩：无 token 的非预登录请求发起前本地拒绝（轮询自动静默停止）', () => {
  const context = apiSetup({ token: null })
  assert.throws(
    () => context.hooks.request({ headers: {}, url: '/reports/executions' }),
    error => error.code === 'ERR_SESSION_EXPIRED'
      && error.message === '登录状态已过期，请重新登录'
      && error.sessionExpired === true,
  )
  assert.throws(() => context.hooks.request({ headers: {}, url: '/devices/' }), { code: 'ERR_SESSION_EXPIRED' })
})

test('请求门闩：预登录端点无 token 放行；有 token 照常带 Bearer', () => {
  const anonymous = apiSetup({ token: null })
  for (const url of ['/auth/token', '/auth/register', '/auth/registration-status', '/client/info', '/auth/token/']) {
    const config = anonymous.hooks.request({ headers: {}, url })
    assert.equal(config.headers.Authorization, undefined, `${url} 预登录放行`)
  }

  const authed = apiSetup({ token: 'tok-9' })
  const config = authed.hooks.request({ headers: {}, url: '/reports/executions' })
  assert.equal(config.headers.Authorization, 'Bearer tok-9')
})

test('api 层清理统一走 useUserStore.logout()，401 呈现可注入且导出 reset 钩子', () => {
  const raw = read('../src/api/index.js')
  assert.match(raw, /import\('@\/stores\/useUserStore'\)/, '清理经 useUserStore（动态 import 避免模块环）')
  assert.match(raw, /useUserStore\(\)\.logout\(\)/, '统一清 pinia token 与 localStorage')
  assert.doesNotMatch(raw, /window\.location\.href\s*=/, '不再直接赋值 window.location.href')
  assert.match(raw, /export const setSessionExpiredHandler/, '会话过期处理器可注入')
  assert.match(raw, /export const resetSessionExpired/, '测试可重置单飞状态')
})

test('sessionExpiry 默认依赖：ElMessageBox.confirm → router.push，清理走 useUserStore.logout()', () => {
  const source = read('../src/utils/sessionExpiry.js')
  assert.match(source, /import\('element-plus'\)[\s\S]*?ElMessageBox\.confirm/, '默认弹框用 ElMessageBox.confirm')
  assert.match(source, /import\('@\/router'\)[\s\S]*?m\.default\.push\(path\)/, '确认后 router.push（SPA 内跳转）')
  assert.match(source, /import\('@\/stores\/useUserStore'\)[\s\S]*?useUserStore\(\)\.logout\(\)/, '默认清理走 useUserStore.logout()')
  assert.match(source, /登录状态已过期，请重新登录/)
})

// ==================== useUserStore.fetchUserInfo rethrow ====================

function userStoreSetup({ getUserInfo } = {}) {
  const stored = []
  const api = {
    login: async () => ({ data: { access_token: 'token-1' } }),
    getUserInfo: getUserInfo || (async () => ({ data: { id: 7, role: 'user' } })),
    getFeatureFlags: async () => ({ data: {} }),
    markLoggedLogout: () => Promise.resolve(),
  }
  const localStorage = {
    getItem: key => (key === 'token' ? (stored.includes('token') ? 'token-1' : null) : null),
    setItem: key => stored.push(key),
    removeItem: key => stored.splice(stored.indexOf(key), 1),
  }
  const source = stripImports(read('../src/stores/useUserStore.js'))
    .replace('export const useUserStore', 'const useUserStore')
  const deps = { defineStore: (_name, factory) => factory, ref, computed, api, localStorage, console: { error: () => {} } }
  const store = new Function(...Object.keys(deps), source + '\nreturn useUserStore()')(...Object.values(deps))
  return { store, stored }
}

test('fetchUserInfo 鉴权失败（401）：rethrow、标记 authFailure、被动清态（路由守卫 catch 真正生效）', async () => {
  const boom = Object.assign(new Error('unauthorized'), { response: { status: 401 } })
  let failing = false
  const { store, stored } = userStoreSetup({
    getUserInfo: async () => { if (failing) throw boom; return { data: { id: 7, role: 'user' } } },
  })
  await store.login('alice', 'secret')
  assert.equal(stored.includes('token'), true)

  failing = true
  await assert.rejects(() => store.fetchUserInfo(), /unauthorized/)
  assert.equal(boom.authFailure, true, '401 按鉴权失败标记')
  assert.equal(stored.includes('token'), false, '被动 logout 清 localStorage')
  assert.equal(store.token.value, '', '被动 logout 清 pinia token')

  // login 链路里 fetchUserInfo 鉴权失败：login 整体失败，不留半迁移态
  const fresh = userStoreSetup({ getUserInfo: async () => { throw boom } })
  await assert.rejects(() => fresh.store.login('alice', 'secret'), /unauthorized/)
  assert.equal(fresh.stored.includes('token'), false)
  assert.equal(fresh.store.token.value, '')
})

test('fetchUserInfo 非鉴权失败（网络抖动/超时）：保会话、标记 authFailure=false、rethrow（F4）', async () => {
  const net = Object.assign(new Error('Network Error'), { request: {} })
  let failing = false
  const { store, stored } = userStoreSetup({
    getUserInfo: async () => { if (failing) throw net; return { data: { id: 7, role: 'user' } } },
  })
  await store.login('alice', 'secret')
  assert.equal(stored.includes('token'), true)

  failing = true
  await assert.rejects(() => store.fetchUserInfo(), /Network Error/)
  assert.equal(net.authFailure, false, '非鉴权错误标记 authFailure=false')
  assert.equal(stored.includes('token'), true, '网络抖动不得清 localStorage token（会话不销毁）')
  assert.equal(store.token.value, 'token-1', 'pinia token 保留')
  assert.equal(store.isLoggedIn.value, true)
})

test('fetchUserInfo 会话门闩错误（ERR_SESSION_EXPIRED）按鉴权失败处理', async () => {
  const gate = Object.assign(new Error('登录状态已过期，请重新登录'), { code: 'ERR_SESSION_EXPIRED', sessionExpired: true })
  let failing = false
  const { store, stored } = userStoreSetup({
    getUserInfo: async () => { if (failing) throw gate; return { data: { id: 7, role: 'user' } } },
  })
  await store.login('alice', 'secret')
  failing = true
  await assert.rejects(() => store.fetchUserInfo())
  assert.equal(gate.authFailure, true, '本地会话门闩属鉴权失败')
  assert.equal(stored.includes('token'), false, '被动 logout 清 localStorage')
  assert.equal(store.token.value, '')
})

test('登录链路：fetchUserInfo 非鉴权失败不回滚登录（token 有效，网络抖动不销毁会话）', async () => {
  const net = Object.assign(new Error('Network Error'), { request: {} })
  let failing = true
  const { store, stored } = userStoreSetup({
    getUserInfo: async () => { if (failing) throw net; return { data: { id: 7, role: 'user' } } },
  })
  const ok = await store.login('alice', 'secret')
  assert.equal(ok, true, 'token 已换到即登录成功，userInfo 由守卫/页面惰性重试')
  assert.equal(stored.includes('token'), true)
  assert.equal(store.token.value, 'token-1')
})

test('fetchUserInfo 支持 silent401：标记透传 getUserInfo（守卫请求不弹会话过期框）', async () => {
  const seen = []
  const { store } = userStoreSetup({
    getUserInfo: async config => { seen.push(config); return { data: { id: 7, role: 'user' } } },
  })
  await store.fetchUserInfo()
  await store.fetchUserInfo({ silent401: true })
  assert.deepEqual(seen, [{ silent401: false }, { silent401: true }], 'silent401 按调用处下传')
})

test('fetchUserInfo 成功仍回填 userInfo 与 featureFlags', async () => {
  const { store } = userStoreSetup()
  store.token.value = 'tok-1'
  await store.fetchUserInfo()
  assert.equal(store.userInfo.value.id, 7)
  assert.equal(store.isLoggedIn.value, true)
  assert.equal(store.featureFlags.value.model_inspection, false)
})

// ==================== 路由守卫 / DeviceCenter 契约 ====================

test('路由守卫：localStorage↔pinia 双向同步；鉴权失败静默收口 /login，非鉴权错误普通路由放行、门禁路由收口首页', () => {
  const source = read('../src/router/index.js')
  assert.match(source, /if \(token && !userStore\.token\) \{\s*userStore\.token = token\s*\}/, 'localStorage→pinia 同步保留')
  assert.match(
    source,
    /if \(!token && userStore\.token\) \{[\s\S]*?userStore\.logout\(\)/,
    'localStorage 无 token 时清 pinia（消除半迁移态）',
  )
  assert.match(
    source,
    /fetchUserInfo\(\{ silent401: true \}\)/,
    '守卫取 userInfo 的请求带 silent401（拦截器不弹「会话过期」框，守卫静默收口）',
  )
  assert.match(
    source,
    /catch \(e\) \{\s*if \(e\?\.authFailure\) \{[\s\S]*?userStore\.logout\(\)\s*next\('\/login'\)\s*\} else \{[\s\S]*?if \(requiresAdmin \|\| requiredFeature\) \{\s*next\('\/'\)\s*\} else \{\s*next\(\)\s*\}\s*\}\s*\}/,
    '鉴权失败 → next(/login)；非鉴权错误 → 门禁路由收口首页、普通路由放行（G5）',
  )
})

// ==================== G5：非鉴权错误下门禁路由不得放行（行为级） ====================

// 从 router/index.js 抽出 beforeEach 处理器本体，依赖注入执行（与 apiSetup 同一套路），
// 真实跑守卫分支，而不是只做源码正则。
const extractRouteGuard = () => {
  const source = read('../src/router/index.js')
  const anchor = source.indexOf('router.beforeEach(')
  assert.ok(anchor >= 0, '找不到路由守卫注册')
  const exprStart = source.indexOf('async (to, from, next) =>', anchor)
  const end = source.indexOf('\n})', exprStart)
  assert.ok(exprStart >= 0 && end > exprStart, '提取守卫处理器失败')
  return source.slice(exprStart, end + 2) // 含箭头函数体的收尾 '}'
}

const runGuard = async ({ to, fetchUserInfo, store = {} }) => {
  const calls = []
  const userStore = {
    token: 'tok-1',
    userInfo: null,
    isAdmin: false,
    featureFlags: {},
    logout: () => calls.push(['logout']),
    fetchUserInfo: fetchUserInfo || (async () => {}),
    ...store,
  }
  const guard = new Function(
    'useUserStore',
    'localStorage',
    `return (${extractRouteGuard()})`,
  )(() => userStore, fakeStorage('tok-1'))
  await guard(to, { path: '/' }, (...args) => calls.push(['next', ...args]))
  return calls
}

const netDownError = () => Object.assign(new Error('network down'), { authFailure: false })

test('G5：非鉴权错误（网络抖动）时 requiresAdmin / featureFlag 路由不放行，收口首页', async () => {
  const throwNet = async () => { throw netDownError() }
  const adminTo = { path: '/settings/users', matched: [{ meta: { requiresAdmin: true } }] }
  const featureTo = { path: '/special/inspection', matched: [{ meta: { featureFlag: 'model_inspection' } }] }

  assert.deepEqual(
    await runGuard({ to: adminTo, fetchUserInfo: throwNet }),
    [['next', '/']],
    '网络抖动时管理员路由不放行（门禁判定不了就不放行）',
  )
  assert.deepEqual(
    await runGuard({ to: featureTo, fetchUserInfo: throwNet }),
    [['next', '/']],
    '网络抖动时 featureFlag 路由不放行',
  )
})

test('G5：非鉴权错误时普通路由照旧放行，且不销毁会话', async () => {
  const throwNet = async () => { throw netDownError() }
  const plainTo = { path: '/dashboard', matched: [{ meta: {} }] }

  const calls = await runGuard({ to: plainTo, fetchUserInfo: throwNet })
  assert.deepEqual(calls, [['next']], '普通路由放行进入（页面级请求自报错）')
  assert.equal(calls.some(c => c[0] === 'logout'), false, '非鉴权错误不销毁会话')
})

test('G5：鉴权失败仍静默收口 /login；userInfo 就绪时门禁按 isAdmin 判定', async () => {
  const throwAuth = async () => { throw Object.assign(new Error('401'), { authFailure: true }) }
  const adminTo = { path: '/settings/users', matched: [{ meta: { requiresAdmin: true } }] }
  const authCalls = await runGuard({ to: adminTo, fetchUserInfo: throwAuth })
  assert.deepEqual(authCalls, [['logout'], ['next', '/login']], '鉴权失败：清态 + 静默收口登录页')

  const ok = async () => {}
  assert.deepEqual(
    await runGuard({ to: adminTo, fetchUserInfo: ok, store: { userInfo: { id: 1 }, isAdmin: false } }),
    [['next', '/']],
    'userInfo 就绪但非管理员：管理员路由收口首页',
  )
  assert.deepEqual(
    await runGuard({ to: adminTo, fetchUserInfo: ok, store: { userInfo: { id: 1 }, isAdmin: true } }),
    [['next']],
    '管理员放行',
  )
})

test('silent401 请求的 401 不弹「会话过期」框；会话中途的后台 401 照常单飞弹框（F5）', async () => {
  const context = apiSetup()
  const seen = []
  context.setSessionExpiredHandler(ctx => { seen.push(ctx) })

  context.hooks.responseFail({ ...error401(), config: { silent401: true } }).catch(() => {})
  await flush()
  assert.deepEqual(seen, [], '守卫请求的 401 静默（弹框的取消=留在当前页语义不受干扰）')

  context.hooks.responseFail(error401()).catch(() => {})
  await flush()
  assert.equal(seen.length, 1, '会话中途的后台 401 照常单飞弹框')
})

test('api.getUserInfo 透传 config、响应拦截器识别 silent401 标记', () => {
  const source = read('../src/api/index.js')
  assert.match(
    source,
    /getUserInfo\(config = \{\}\) \{\s*return api\.get\('\/auth\/users\/me', config\)/,
    'getUserInfo 接受 per-call config（silent401 随请求下传）',
  )
  assert.match(
    source,
    /error\.response\.status === 401 && !error\.config\?\.silent401/,
    'silent401 请求的 401 跳过会话过期单飞处理',
  )
})

test('DeviceCenter：autoRefresh 由 onActivated/onDeactivated 管理，onBeforeUnmount 兜底，token 门闩静默停表', () => {
  const source = read('../src/views/devices/DeviceCenter.vue')
  assert.match(source, /onActivated\(\(\) => \{\s*startAutoRefresh\(\)\s*\}\)/, 'KeepAlive 激活恢复轮询')
  assert.match(source, /onDeactivated\(\(\) => \{\s*stopAutoRefresh\(\)\s*\}\)/, '离开即停表')
  assert.match(source, /onBeforeUnmount\(\(\) => \{\s*stopAutoRefresh\(\)\s*\}\)/, '卸载兜底清理')
  assert.match(
    source,
    /const fetchDevices = async [\s\S]*?if \(!hasSessionToken\(\)\) return/,
    '设备列表请求发起前检查 token（401 后静默停止）',
  )
  assert.match(
    source,
    /if \(!hasSessionToken\(\)\) \{\s*stopAutoRefresh\(\)\s*return\s*\}/,
    '轮询 tick 发现 token 已清即自动停表',
  )
  assert.match(source, /const fetchRemoteAgents = async \(\) => \{\s*if \(!hasSessionToken\(\)\) return/)
})
