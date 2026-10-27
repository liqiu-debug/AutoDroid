import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, ref } from 'vue'

// S9：客户端（桌面引擎）模式下 iOS IPA 一键安装恒 501，入口必须隐藏/禁用并降级提示
// 「iOS 安装请用平台/后续版本」；纯 Web 模式行为零变化。
// 与仓库既有测试一致，用 node:test + 依赖注入执行 <script setup>（非 vitest，见 README/CI）。

const stripImports = source => source.replace(/^import [\s\S]*? from ['"][^'"]+['"]\s*\n/gm, '')
const vueSource = name => readFileSync(new URL(`../src/views/${name}`, import.meta.url), 'utf8')

const messages = record => Object.fromEntries(
  ['success', 'warning', 'error', 'info'].map(kind => [kind, (...args) => record.push([kind, ...args])])
)

function composableSetup({ clientInfo } = {}) {
  const calls = []
  const api = {
    getClientInfo: async () => {
      calls.push(['clientInfo'])
      if (clientInfo instanceof Error) throw clientInfo
      return { data: clientInfo }
    }
  }
  const source = stripImports(readFileSync(new URL('../src/composables/useClientApp.js', import.meta.url), 'utf8'))
    .replace('export function useClientApp', 'function useClientApp')
  const deps = { ref, api }
  const useClientApp = new Function(...Object.keys(deps), source + '\nreturn useClientApp')(...Object.values(deps))
  return { useClientApp, calls }
}

function packageSetup({ clientMode } = {}) {
  const apiCalls = []
  const notices = []
  const mounted = []
  const installRequests = []
  const api = {
    getPackages: async () => { apiCalls.push(['getPackages']); return { data: { items: [], total: 0 } } },
    getDeviceList: async () => { apiCalls.push(['getDeviceList']); return { data: [{ serial: 'ios:udid', platform: 'ios', status: 'IDLE' }] } },
    installPackage: async (id, serial) => { installRequests.push([id, serial]); return { data: { msg: 'ok' } } },
  }
  const isClientApp = ref(!!clientMode)
  const detectClientApp = async () => {
    apiCalls.push(['detectClientApp'])
    return isClientApp.value
  }
  const script = stripImports(vueSource('packages/PackageManagement.vue').match(/<script setup>([\s\S]*?)<\/script>/)[1])
  const deps = {
    ref, computed,
    onMounted: fn => mounted.push(fn),
    ElMessage: messages(notices),
    ElMessageBox: { confirm: async () => true },
    api,
    useClientApp: () => ({ isClientApp, detectClientApp }),
  }
  const exports = 'openInstallDialog,handleInstall,isIosInstallBlocked,installDialogVisible,installTarget,deviceList,selectedSerial'
  const state = new Function(...Object.keys(deps), script + `\nreturn {${exports}}`)(...Object.values(deps))
  return { state, apiCalls, notices, mounted, installRequests, isClientApp }
}

test('client info probe caches one fetch and maps web mode to false', async () => {
  const { useClientApp, calls } = composableSetup({ clientInfo: { clientMode: true } })
  const app = useClientApp()
  assert.equal(await app.detectClientApp(), true)
  assert.equal(app.isClientApp.value, true)
  assert.equal(app.probeDone.value, true)
  await app.detectClientApp()
  assert.equal(calls.length, 1, 'probe is idempotent')

  const web = composableSetup({ clientInfo: new Error('404') })
  const webApp = web.useClientApp()
  assert.equal(await webApp.detectClientApp(), false)
  assert.equal(webApp.isClientApp.value, false)
})

test('client mode refuses the iOS install dialog with the degradation notice', async () => {
  const { state, apiCalls, notices, installRequests } = packageSetup({ clientMode: true })

  await state.openInstallDialog({ id: 3, platform: 'ios', app_name: 'iOS应用' })
  assert.equal(state.installDialogVisible.value, false, 'dialog must not open on the 501 dead end')
  assert.equal(state.installTarget.value, null)
  assert.deepEqual(notices, [['info', 'iOS 安装请用平台/后续版本']])
  assert.ok(!apiCalls.some(([name]) => name === 'getDeviceList'), 'no device fetch behind a dead entry')
  assert.deepEqual(installRequests, [])

  // Android 安装入口不受影响
  await state.openInstallDialog({ id: 4, platform: 'android', app_name: 'Android应用' })
  assert.equal(state.installDialogVisible.value, true)
  assert.equal(state.installTarget.value.id, 4)
})

test('web mode keeps the iOS install flow unchanged', async () => {
  const { state, notices, apiCalls } = packageSetup({ clientMode: false })

  await state.openInstallDialog({ id: 3, platform: 'ios', app_name: 'iOS应用' })
  assert.equal(state.installDialogVisible.value, true)
  assert.equal(state.installTarget.value.id, 3)
  assert.deepEqual(notices, [])
  assert.ok(apiCalls.some(([name]) => name === 'getDeviceList'))
})

test('package list gates the iOS install entry and shows the degradation hint', () => {
  const source = vueSource('packages/PackageManagement.vue')
  assert.match(source, /v-if="isIosInstallBlocked\(row\)"/, 'blocked branch must replace the active install button')
  assert.match(source, /title="iOS 安装请用平台\/后续版本"/)
  assert.match(source, /<el-button v-else-if="!isDesktopPackage\(row\)" type="primary" link :icon="Cellphone" @click="openInstallDialog\(row\)">/)
  assert.match(source, /await detectClientApp\(\)\n  if \(isIosInstallBlocked\(row\)\)/, 'dialog guard settles the probe before gating')
  assert.match(source, /const isIosInstallBlocked = \(row\) => isClientApp\.value && \(row\?\.platform \|\| 'android'\) === 'ios'/)
  assert.match(source, /detectClientApp\(\)\n  fetchPackages\(\)/, 'probe runs on mount alongside the package list')
})
