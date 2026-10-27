import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { ref, computed, reactive } from 'vue'

// 登录页「客户端模式」契约测试：
// - GET /api/client/info 200 = 客户端（桌面引擎）模式，否则纯 Web（UI/行为零变化）
// - 客户端模式登录带 X-AutoDroid-Server 头，成功后按「保存信息/自动登录」走 window.desktop
// - 纯 Web 模式不渲染服务器地址与勾选框、不碰桌面桥
// 与仓库既有测试一致，用 node:test + 依赖注入执行 <script setup>（非 vitest，见 README/CI）。

const stripImports = source => source.replace(/^import [\s\S]*? from ['"][^'"]+['"]\s*\n/gm, '')
const messages = record => Object.fromEntries(['success', 'warning', 'error', 'info'].map(kind => [kind, (...args) => record.push([kind, ...args])]))
const vueSource = name => readFileSync(new URL(`../src/views/${name}`, import.meta.url), 'utf8')

function apiSetup({ desktop } = {}) {
  const calls = []
  const instance = {
    interceptors: { request: { use: () => {} }, response: { use: () => {} } },
    get: (...args) => { calls.push(['get', ...args]); return Promise.resolve({ data: {} }) },
    post: (...args) => { calls.push(['post', ...args]); return Promise.resolve({ data: {} }) },
    put: (...args) => { calls.push(['put', ...args]); return Promise.resolve({ data: {} }) },
    patch: (...args) => { calls.push(['patch', ...args]); return Promise.resolve({ data: {} }) },
    delete: (...args) => { calls.push(['delete', ...args]); return Promise.resolve({ data: {} }) },
  }
  const source = stripImports(readFileSync(new URL('../src/api/index.js', import.meta.url), 'utf8'))
    .replace('export default', 'const __default =')
    .replace(/export const/g, 'const')
  const deps = { axios: { create: () => instance }, normalizeError: () => {}, window: desktop === undefined ? {} : { desktop } }
  const exposed = new Function(...Object.keys(deps), source + '\nreturn { LOGIN_NOTICE_KEY, api: __default }')(...Object.values(deps))
  return { ...exposed, calls, instance }
}

function userStoreSetup() {
  const calls = []
  const stored = []
  const api = {
    login: async (params, options) => {
      calls.push(['login', String(params), options])
      return { data: { access_token: 'token-1' } }
    },
    getUserInfo: async () => ({ data: { id: 7, role: 'user' } }),
    getFeatureFlags: async () => ({ data: {} }),
    markLoggedLogout: (...args) => { calls.push(['markLoggedLogout', ...args]); return Promise.resolve() },
  }
  const localStorage = {
    getItem: key => (key === 'token' ? (stored.includes('token') ? 'token-1' : null) : null),
    setItem: key => stored.push(key),
    removeItem: key => stored.splice(stored.indexOf(key), 1),
  }
  const source = stripImports(readFileSync(new URL('../src/stores/useUserStore.js', import.meta.url), 'utf8'))
    .replace('export const useUserStore', 'const useUserStore')
  const deps = { defineStore: (_name, factory) => factory, ref, computed, api, localStorage, console: { error: () => {} } }
  const store = new Function(...Object.keys(deps), source + '\nreturn useUserStore()')(...Object.values(deps))
  return { store, calls, stored }
}

function loginSetup({ clientInfo, savedLogin, saveLogin, validate = async () => true } = {}) {
  const apiCalls = []
  const storeCalls = []
  const navigations = []
  const notices = []
  const mounted = []
  const storage = new Map()
  const api = {
    getRegistrationStatus: async () => ({ data: { allow_registration: true } }),
    getClientInfo: async () => {
      apiCalls.push(['clientInfo'])
      if (clientInfo instanceof Error) throw clientInfo
      return { data: clientInfo }
    },
    getSavedLogin: async () => { apiCalls.push(['getSavedLogin']); return savedLogin ?? null },
    saveLogin: async data => { apiCalls.push(['saveLogin', data]); if (saveLogin instanceof Error) throw saveLogin },
  }
  const userStore = {
    login: async (username, password, options) => {
      storeCalls.push(['login', username, password, options])
      return true
    },
  }
  const script = stripImports(vueSource('LoginView.vue').match(/<script setup>([\s\S]*?)<\/script>/)[1])
  const deps = {
    computed, reactive, ref,
    onMounted: fn => mounted.push(fn),
    useRouter: () => ({ push: path => navigations.push(path) }),
    useUserStore: () => userStore,
    useClientMode: () => ({ isMobileMode: ref(false) }),
    User: 'User', Lock: 'Lock', Connection: 'Connection',
    ElMessage: messages(notices),
    api, LOGIN_NOTICE_KEY: 'autodroid.login_notice',
    describeError: error => error?.message || '',
    ClientModeSwitch: {},
    sessionStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key),
    },
  }
  const exports = 'handleLogin,loginForm,loginRules,isClientApp,clientProbeDone,probeFailed,rememberInfo,autoLogin,loading,loginFormRef,allowRegistration,onRememberInfoChange,detectClientApp,persistLogin'
  const state = new Function(...Object.keys(deps), script + `\nreturn {${exports}}`)(...Object.values(deps))
  state.loginFormRef.value = { validate }
  return { state, apiCalls, storeCalls, navigations, notices, mounted, api, userStore }
}

const mount = async context => { await context.mounted[0]() }

test('login api layer sends X-AutoDroid-Server only when a client-mode server is given', async () => {
  const { api, calls } = apiSetup()
  const form = new URLSearchParams([['username', 'alice'], ['password', 'secret']])
  await api.login(form, { server: 'http://127.0.0.1:8000/' })
  assert.equal(calls[0][0], 'post')
  assert.equal(calls[0][1], '/auth/token')
  assert.equal(calls[0][2], form)
  assert.deepEqual(calls[0][3], { headers: { 'X-AutoDroid-Server': 'http://127.0.0.1:8000/' } })

  calls.length = 0
  await api.login(form)
  assert.deepEqual(calls[0][3], {}, 'web mode keeps the exact legacy request shape')
  calls.length = 0
  await api.login(form, { server: '   ' })
  assert.deepEqual(calls[0][3], {}, 'blank server never leaks an empty header')
})

test('client info probe and desktop bridge degrade cleanly without window.desktop', async () => {
  const { api, calls } = apiSetup()
  await api.getClientInfo()
  assert.deepEqual(calls[0], ['get', '/client/info'])
  assert.equal(await api.getSavedLogin(), null)
  await api.saveLogin({ server: 'x' })
  await api.saveLogin(null)
  await api.markLoggedLogout(true) // 无桥静默 no-op，不得抛错

  const saved = { server: 'https://upstream', username: 'alice', password: 'p', rememberInfo: true, autoLogin: true }
  const bridgeCalls = []
  const bridged = apiSetup({
    desktop: {
      getSavedLogin: async () => { bridgeCalls.push(['getSavedLogin']); return saved },
      saveLogin: async data => { bridgeCalls.push(['saveLogin', data]) },
      markLoggedOut: async () => { bridgeCalls.push(['markLoggedOut']) },
    },
  })
  assert.deepEqual(await bridged.api.getSavedLogin(), saved)
  await bridged.api.saveLogin(null)
  await bridged.api.saveLogin(saved)
  await bridged.api.markLoggedLogout(true)
  assert.deepEqual(bridgeCalls, [['getSavedLogin'], ['saveLogin', null], ['saveLogin', saved], ['markLoggedOut']])
})

test('user store forwards the upstream server to the login api call', async () => {
  const { store, calls, stored } = userStoreSetup()
  await store.login('alice', 'secret', { server: 'https://upstream.example.com' })
  assert.equal(calls.length, 1)
  assert.equal(calls[0][0], 'login')
  assert.match(calls[0][1], /username=alice/)
  assert.match(calls[0][1], /password=secret/)
  assert.deepEqual(calls[0][2], { server: 'https://upstream.example.com' })
  assert.equal(store.token.value, 'token-1')
  assert.ok(stored.includes('token'))

  calls.length = 0
  await store.login('bob', 'pw')
  assert.deepEqual(calls[0][2], { server: undefined }, 'web mode login carries no upstream')
})

test('web mode keeps the login UI and behavior unchanged and never touches the desktop bridge', async () => {
  const source = vueSource('LoginView.vue')
  assert.match(source, /<el-form-item v-if="isClientApp" label="服务器地址" prop="serverAddress">/)
  assert.match(source, /<el-form-item v-if="isClientApp" class="login-options">/)
  assert.ok(source.includes('v-model="rememberInfo" @change="onRememberInfoChange">保存信息</el-checkbox>'))
  assert.ok(source.includes('v-model="autoLogin" :disabled="!rememberInfo">自动登录</el-checkbox>'))
  assert.ok(source.includes('<router-link v-if="clientProbeDone && allowRegistration && !isClientApp && !probeFailed" to="/register"'), 'client mode hides the register link')

  const context = loginSetup({ clientInfo: Object.assign(new Error('Request failed with status code 404'), { response: { status: 404 } }) })
  context.state.loginForm.username = 'alice'
  context.state.loginForm.password = 'secret'
  await mount(context)
  assert.equal(context.state.isClientApp.value, false)
  assert.equal(context.apiCalls.some(call => call[0] === 'getSavedLogin'), false)
  await context.state.handleLogin()
  assert.deepEqual(context.storeCalls, [['login', 'alice', 'secret', { server: undefined }]])
  assert.equal(context.apiCalls.some(call => call[0] === 'saveLogin'), false, 'no bridge calls in pure web mode')
  assert.deepEqual(context.navigations, ['/'])
})

test('client mode probe enables the server field and requires the server address', async () => {
  const context = loginSetup({ clientInfo: { clientMode: true, platform: 'darwin', version: '1.2.3' } })
  await mount(context)
  assert.equal(context.state.isClientApp.value, true)
  const rules = context.state.loginRules
  assert.ok(rules.serverAddress, 'server address is required in client mode')
  assert.equal(rules.serverAddress[0].required, true)
  assert.ok(rules.username && rules.password)

  // 服务地址规则始终存在；纯 Web 模式该 form-item 未挂载，规则天然不生效。
  const web = loginSetup({ clientInfo: { clientMode: false } })
  await mount(web)
  assert.equal(web.state.isClientApp.value, false)
  assert.ok(web.state.loginRules.serverAddress, 'rules stay complete in web mode (inert field)')
  assert.equal(web.state.loginRules.serverAddress[0].required, true)
})

test('login rules are a stable plain constant so validateOnRuleChange never fires on probe', async () => {
  const source = vueSource('LoginView.vue')
  assert.match(source, /const loginRules = \{/, 'loginRules must be a plain constant, not a computed')
  assert.ok(!/const loginRules = computed\(/.test(source), 'a computed rules object re-identifies and triggers first-screen validation errors')

  const context = loginSetup({ clientInfo: { clientMode: true, platform: 'darwin', version: '1.2.3' } })
  const rulesBeforeProbe = context.state.loginRules
  await mount(context) // isClientApp flips false → true here; rules identity must not change
  assert.equal(context.state.isClientApp.value, true)
  assert.equal(context.state.loginRules, rulesBeforeProbe, 'rules reference is stable across the client-mode probe')
  assert.equal(typeof rulesBeforeProbe, 'object')
  assert.ok(!('value' in rulesBeforeProbe), 'not a computed ref')
})

test('client mode login sends the server header and persists the full login on success', async () => {
  const context = loginSetup({ clientInfo: { clientMode: true, platform: 'win32', version: '1.2.3' } })
  await mount(context)
  context.state.rememberInfo.value = true
  context.state.loginForm.serverAddress = ' https://upstream.example.com/ '
  context.state.loginForm.username = 'alice'
  context.state.loginForm.password = 'secret'
  await context.state.handleLogin()
  // 前端只做 trim；尾部 / 等归一化由本地引擎按 ServerAddress 语义处理
  assert.deepEqual(context.storeCalls, [['login', 'alice', 'secret', { server: 'https://upstream.example.com/' }]])
  assert.deepEqual(context.apiCalls.filter(call => call[0] === 'saveLogin'), [[
    'saveLogin',
    {
      server: 'https://upstream.example.com/',
      username: 'alice',
      password: 'secret',
      rememberInfo: true,
      autoLogin: false,
    },
  ]])
  assert.deepEqual(context.navigations, ['/'])
  assert.ok(context.notices.some(([kind]) => kind === 'success'))
})

test('unchecking 保存信息 clears the saved login and resets 自动登录', async () => {
  const context = loginSetup({ clientInfo: { clientMode: true, platform: 'darwin', version: '1.2.3' } })
  await mount(context)
  context.state.rememberInfo.value = true
  context.state.autoLogin.value = true
  context.state.rememberInfo.value = false // v-model 先更新，再触发 change
  context.state.onRememberInfoChange(false)
  assert.equal(context.state.autoLogin.value, false, 'auto login depends on 保存信息')

  context.state.loginForm.username = 'alice'
  context.state.loginForm.password = 'secret'
  context.state.loginForm.serverAddress = 'http://127.0.0.1:8000'
  await context.state.handleLogin()
  assert.deepEqual(context.apiCalls.filter(call => call[0] === 'saveLogin'), [['saveLogin', null]])
  assert.deepEqual(context.storeCalls, [['login', 'alice', 'secret', { server: 'http://127.0.0.1:8000' }]])
})

test('saved login without auto login prefills the form but never submits', async () => {
  const context = loginSetup({
    clientInfo: { clientMode: true, platform: 'darwin', version: '1.2.3' },
    savedLogin: { server: 'https://upstream', username: 'alice', password: 'saved-pw', rememberInfo: true, autoLogin: false },
  })
  await mount(context)
  assert.deepEqual(
    [context.state.loginForm.serverAddress, context.state.loginForm.username, context.state.loginForm.password],
    ['https://upstream', 'alice', 'saved-pw'],
  )
  assert.equal(context.state.rememberInfo.value, true)
  assert.equal(context.state.autoLogin.value, false)
  assert.equal(context.storeCalls.length, 0)
})

test('saved login with auto login prefills and submits the login automatically', async () => {
  const context = loginSetup({
    clientInfo: { clientMode: true, platform: 'darwin', version: '1.2.3' },
    savedLogin: { server: 'https://upstream.example.com', username: 'alice', password: 'saved-pw', rememberInfo: true, autoLogin: true },
  })
  await mount(context)
  assert.deepEqual(context.storeCalls, [['login', 'alice', 'saved-pw', { server: 'https://upstream.example.com' }]])
  assert.deepEqual(context.navigations, ['/'])
  const saved = context.apiCalls.filter(call => call[0] === 'saveLogin')[0]
  assert.deepEqual(saved[1], {
    server: 'https://upstream.example.com',
    username: 'alice',
    password: 'saved-pw',
    rememberInfo: true,
    autoLogin: true,
  })
})

test('login failure keeps the shared error wording and does not touch the bridge', async () => {
  const context = loginSetup({ clientInfo: { clientMode: true, platform: 'darwin', version: '1.2.3' } })
  context.userStore.login = async () => { throw { message: '用户名或密码错误', response: { status: 400, data: { detail: '用户名或密码错误' } } } }
  context.state.loginForm.serverAddress = 'http://127.0.0.1:8000'
  context.state.loginForm.username = 'alice'
  context.state.loginForm.password = 'bad'
  await mount(context)
  await context.state.handleLogin()
  assert.deepEqual(context.notices.filter(([kind]) => kind === 'error'), [['error', '用户名或密码错误']])
  assert.equal(context.apiCalls.some(call => call[0] === 'saveLogin'), false)
  assert.deepEqual(context.navigations, [])
})

test('failed form validation never sends the login request', async () => {
  const context = loginSetup({
    clientInfo: { clientMode: true, platform: 'darwin', version: '1.2.3' },
    validate: async () => { throw new Error('invalid fields') },
  })
  await mount(context)
  await context.state.handleLogin()
  assert.equal(context.storeCalls.length, 0)
})

test('register link is gated on clientProbeDone and hidden before the probe settles', async () => {
  const context = loginSetup({ clientInfo: Object.assign(new Error('Request failed with status code 404'), { response: { status: 404 } }) })
  assert.equal(context.state.clientProbeDone.value, false, 'gate is closed before the client probe settles')
  await mount(context)
  assert.equal(context.state.clientProbeDone.value, true, 'gate opens once the client probe settles')
  assert.equal(context.state.probeFailed.value, false)
  assert.equal(context.state.isClientApp.value, false)
  assert.equal(context.state.allowRegistration.value, true, 'web mode keeps registration available after the probe')
})

test('register link stays hidden when the client probe fails (non-404)', async () => {
  const context = loginSetup({ clientInfo: new Error('Network Error') })
  await mount(context)
  assert.equal(context.state.probeFailed.value, true, 'a transport failure cannot confirm a registration channel')
  assert.equal(context.state.clientProbeDone.value, true)
  assert.equal(context.state.isClientApp.value, false)
  // link gate: clientProbeDone && allowRegistration && !isClientApp && !probeFailed → false (probeFailed hides entry)
  // login itself is unaffected by the registration gate
  context.state.loginForm.username = 'alice'
  context.state.loginForm.password = 'secret'
  await context.state.handleLogin()
  assert.deepEqual(context.storeCalls, [['login', 'alice', 'secret', { server: undefined }]])
})

test('saveLogin failure warns the user instead of swallowing the error silently', async () => {
  const source = vueSource('LoginView.vue')
  assert.match(source, /登录成功，但保存登录信息失败/, 'persistLogin failure must surface a warning')

  const context = loginSetup({
    clientInfo: { clientMode: true, platform: 'darwin', version: '1.2.3' },
    saveLogin: new Error('bridge write failed'),
  })
  await mount(context)
  context.state.rememberInfo.value = true
  context.state.loginForm.serverAddress = 'http://127.0.0.1:8000'
  context.state.loginForm.username = 'alice'
  context.state.loginForm.password = 'secret'
  await context.state.handleLogin()
  assert.deepEqual(context.storeCalls, [['login', 'alice', 'secret', { server: 'http://127.0.0.1:8000' }]], 'login itself still succeeds')
  assert.ok(
    context.notices.some(([kind, message]) => kind === 'warning' && message === '登录成功，但保存登录信息失败'),
    `expected the save-failure warning, got ${JSON.stringify(context.notices)}`,
  )
  assert.deepEqual(context.navigations, ['/'], 'the user still lands in the workspace')

  // 清除保存失败同样要提示，而不是静默
  const clearing = loginSetup({
    clientInfo: { clientMode: true, platform: 'darwin', version: '1.2.3' },
    saveLogin: new Error('bridge clear failed'),
  })
  await mount(clearing)
  clearing.state.loginForm.serverAddress = 'http://127.0.0.1:8000'
  clearing.state.loginForm.username = 'alice'
  clearing.state.loginForm.password = 'secret'
  await clearing.state.handleLogin()
  assert.deepEqual(clearing.apiCalls.filter(call => call[0] === 'saveLogin'), [['saveLogin', null]])
  assert.ok(clearing.notices.some(([kind, message]) => kind === 'warning' && message === '登录成功，但保存登录信息失败'))
})

test('saved login with loggedOut=true never auto-submits even when autoLogin is on', async () => {
  // 手动退出登录后（loggedOut=true）跨重启也不再自动登录，直到用户手动重新登录并勾选自动登录。
  const context = loginSetup({
    clientInfo: { clientMode: true, platform: 'darwin', version: '1.2.3' },
    savedLogin: {
      server: 'https://upstream.example.com',
      username: 'alice',
      password: 'saved-pw',
      rememberInfo: true,
      autoLogin: true,
      loggedOut: true,
    },
  })
  await mount(context)
  assert.equal(context.storeCalls.length, 0, 'loggedOut=true 必须阻断自动登录')
  assert.deepEqual(context.navigations, [])
  // 凭据仍回填，用户可手动登录
  assert.deepEqual(
    [context.state.loginForm.serverAddress, context.state.loginForm.username, context.state.loginForm.password],
    ['https://upstream.example.com', 'alice', 'saved-pw'],
  )
  assert.equal(context.storeCalls.some(call => call[0] === 'login'), false)
})

test('manual logout(true) calls markLoggedLogout with true; plain logout never does', async () => {
  const { store, calls, stored } = userStoreSetup()
  await store.login('alice', 'secret')
  calls.length = 0

  store.logout() // 401/取用户信息失败等被动登出：manual 默认 false，不置标记
  assert.deepEqual(calls, [], '默认 logout() 不得调 markLoggedLogout')
  assert.equal(stored.includes('token'), false)

  await store.login('alice', 'secret')
  calls.length = 0
  store.logout(true) // 用户主动退出：markLoggedLogout(true) 传参
  assert.deepEqual(calls, [['markLoggedLogout', true]])
  assert.equal(store.token.value, '')
  assert.equal(stored.includes('token'), false)
})

test('markLoggedLogout only fires for manual sign-out and is swallowed on bridge failure', async () => {
  // 桥调用失败必须静默，绝不影响登出本身
  const calls = []
  const stored = []
  const api = {
    login: async () => ({ data: { access_token: 'token-1' } }),
    getUserInfo: async () => ({ data: { id: 7, role: 'user' } }),
    getFeatureFlags: async () => ({ data: {} }),
    markLoggedLogout: () => { calls.push(['markLoggedLogout']); return Promise.reject(new Error('bridge gone')) },
  }
  const localStorage = {
    getItem: key => (key === 'token' ? (stored.includes('token') ? 'token-1' : null) : null),
    setItem: key => stored.push(key),
    removeItem: key => stored.splice(stored.indexOf(key), 1),
  }
  const source = stripImports(readFileSync(new URL('../src/stores/useUserStore.js', import.meta.url), 'utf8'))
    .replace('export const useUserStore', 'const useUserStore')
  const deps = { defineStore: (_name, factory) => factory, ref, computed, api, localStorage, console: { error: () => {} } }
  const store = new Function(...Object.keys(deps), source + '\nreturn useUserStore()')(...Object.values(deps))
  await store.login('alice', 'secret')
  calls.length = 0
  store.logout(true)
  assert.deepEqual(calls, [['markLoggedLogout']], '失败也已尝试调用')
  assert.equal(store.token.value, '', '标记失败不影响登出结果')
})
