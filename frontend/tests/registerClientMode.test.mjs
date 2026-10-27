import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { ref, reactive } from 'vue'

// 注册页「客户端模式」契约测试：
// - 客户端（桌面引擎）模式下注册链路不可用（无 X-AutoDroid-Server/upstream）：
//   顶部提示「客户端模式请通过平台开号」，隐藏注册表单并阻止提交
// - 纯 Web 模式行为与原先完全一致（allow_registration 关闭 → 提示联系管理员）
// 与仓库既有测试一致，用 node:test + 依赖注入执行 <script setup>（非 vitest）。

const stripImports = source => source.replace(/^import [\s\S]*? from ['"][^'"]+['"]\s*\n/gm, '')
const messages = record => Object.fromEntries(['success', 'warning', 'error', 'info'].map(kind => [kind, (...args) => record.push([kind, ...args])]))
const registerSource = () => readFileSync(new URL('../src/views/login/Register.vue', import.meta.url), 'utf8')

function registerSetup({ clientInfo, registration = { allow_registration: true }, registerImpl } = {}) {
  const apiCalls = []
  const navigations = []
  const notices = []
  const mounted = []
  const api = {
    getClientInfo: async () => {
      apiCalls.push(['clientInfo'])
      if (clientInfo instanceof Error) throw clientInfo
      return { data: clientInfo }
    },
    getRegistrationStatus: async () => {
      apiCalls.push(['getRegistrationStatus'])
      return { data: registration }
    },
    register: async data => {
      apiCalls.push(['register', data])
      if (registerImpl instanceof Error) throw registerImpl
      return { data: {} }
    },
  }
  const script = stripImports(registerSource().match(/<script setup>([\s\S]*?)<\/script>/)[1])
  const deps = {
    reactive, ref,
    onMounted: fn => mounted.push(fn),
    useRouter: () => ({ push: path => navigations.push(path) }),
    useClientMode: () => ({ isMobileMode: ref(false) }),
    User: 'User', Lock: 'Lock', Avatar: 'Avatar',
    ElMessage: messages(notices),
    api,
  }
  const exports = 'handleRegister,form,rules,formRef,loading,registrationAllowed,isClientApp,clientProbeDone,probeFailed,detectClientApp,loadRegistrationStatus,validatePass2'
  const state = new Function(...Object.keys(deps), script + `\nreturn {${exports}}`)(...Object.values(deps))
  state.formRef.value = { validate: async cb => cb(true) }
  return { state, apiCalls, navigations, notices, mounted, api }
}

const mount = async context => { await context.mounted[0]() }

test('register page hides the form and blocks submission in client mode', async () => {
  const source = registerSource()
  assert.match(source, /title="客户端模式请通过平台开号，请联系管理员开号。"/, 'client mode shows the platform-provisioned-account notice')
  assert.match(source, /<el-form\n\s+v-if="clientProbeDone && registrationAllowed && !isClientApp && !probeFailed"/, 'client mode must not render the register form')
  assert.match(source, /if \(isClientApp\.value\) \{[\s\S]*?客户端模式请通过平台开号[\s\S]*?return/, 'handleRegister must refuse in client mode')

  const context = registerSetup({ clientInfo: { clientMode: true, platform: 'darwin', version: '1.2.3' } })
  await mount(context)
  assert.equal(context.state.isClientApp.value, true)
  assert.equal(context.state.registrationAllowed.value, true, 'server-side allow_registration is irrelevant when the entry is hidden')

  context.state.form.username = 'alice'
  context.state.form.name = 'Alice'
  context.state.form.password = 'secret1'
  context.state.form.confirmPassword = 'secret1'
  await context.state.handleRegister()
  assert.equal(context.apiCalls.some(call => call[0] === 'register'), false, 'submitting must never reach the register API')
  assert.ok(
    context.notices.some(([kind, message]) => kind === 'warning' && message.includes('客户端模式请通过平台开号')),
    `expected the platform-notice warning, got ${JSON.stringify(context.notices)}`,
  )
  assert.deepEqual(context.navigations, [])
})

test('register link is hidden on the login page in client mode', () => {
  const loginSource = readFileSync(new URL('../src/views/LoginView.vue', import.meta.url), 'utf8')
  assert.ok(loginSource.includes('<router-link v-if="clientProbeDone && allowRegistration && !isClientApp && !probeFailed" to="/register"'))
})

test('web mode keeps the register flow unchanged', async () => {
  const context = registerSetup({ clientInfo: Object.assign(new Error('Request failed with status code 404'), { response: { status: 404 } }) })
  await mount(context)
  assert.equal(context.state.isClientApp.value, false)
  assert.equal(context.state.registrationAllowed.value, true)

  context.state.form.username = 'alice'
  context.state.form.name = 'Alice'
  context.state.form.password = 'secret1'
  context.state.form.confirmPassword = 'secret1'
  await context.state.handleRegister()
  assert.deepEqual(context.apiCalls.filter(call => call[0] === 'register'), [[
    'register',
    { username: 'alice', name: 'Alice', password: 'secret1' },
  ]])
  assert.ok(context.notices.some(([kind]) => kind === 'success'))
  assert.deepEqual(context.navigations, ['/login'])
})

test('web mode with registration closed still blocks with the admin notice', async () => {
  const context = registerSetup({
    clientInfo: Object.assign(new Error('Request failed with status code 404'), { response: { status: 404 } }),
    registration: { allow_registration: false },
  })
  await mount(context)
  assert.equal(context.state.registrationAllowed.value, false)
  await context.state.handleRegister()
  assert.equal(context.apiCalls.some(call => call[0] === 'register'), false)
  assert.ok(context.notices.some(([kind, message]) => kind === 'warning' && message.includes('请联系管理员创建账号')))
})

test('registration entry is gated on clientProbeDone: hidden before the probe settles', async () => {
  const source = registerSource()
  assert.match(source, /v-if="clientProbeDone && isClientApp"/, 'client notice waits for the probe to settle')
  assert.match(source, /v-else-if="clientProbeDone && probeFailed"/, 'probe-failure notice waits for the probe to settle')
  assert.match(source, /v-if="clientProbeDone && registrationAllowed && !isClientApp && !probeFailed"/, 'form is gated behind clientProbeDone')

  const context = registerSetup({ clientInfo: { clientMode: true, platform: 'darwin', version: '1.2.3' } })
  assert.equal(context.state.clientProbeDone.value, false, 'gate is closed before the client probe settles')
  await mount(context)
  assert.equal(context.state.clientProbeDone.value, true, 'gate opens once the client probe settles')
  assert.equal(context.state.probeFailed.value, false)
})

test('probe failure keeps the admin notice and never offers registration', async () => {
  const source = registerSource()
  assert.match(source, /title="暂时无法确认注册通道，请联系管理员开号。"/, 'probe failure keeps the 请联系管理员开号 notice')

  // 传输失败（无 response）→ 无法判定注册通道，维持「请联系管理员开号」，不放行注册入口。
  const context = registerSetup({ clientInfo: new Error('Network Error') })
  await mount(context)
  assert.equal(context.state.probeFailed.value, true, 'a non-404 failure marks the probe as failed')
  assert.equal(context.state.clientProbeDone.value, true)
  assert.equal(context.state.isClientApp.value, false)

  context.state.form.username = 'alice'
  context.state.form.name = 'Alice'
  context.state.form.password = 'secret1'
  context.state.form.confirmPassword = 'secret1'
  await context.state.handleRegister()
  assert.equal(context.apiCalls.some(call => call[0] === 'register'), false, 'submitting must never reach the register API on probe failure')
  assert.ok(
    context.notices.some(([kind, message]) => kind === 'warning' && message.includes('请联系管理员开号')),
    `expected the admin notice, got ${JSON.stringify(context.notices)}`,
  )
  assert.deepEqual(context.navigations, [])
})
