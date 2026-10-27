import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, reactive, ref, nextTick } from 'vue'
import {
  VERSION_CONFLICT_MESSAGE,
  extractVersionState,
  isVersionConflict,
  versionRequestConfig,
  versionStateFrom,
  versionTokenFrom,
} from '../src/utils/versionToken.js'
import * as actionConstants from '../src/utils/actionConstants.js'
import * as statusMeta from '../src/utils/statusMeta.js'

// P4 功能1（步骤写入乐观锁）前端侧：PUT /api/cases/{id}/steps 与场景 steps 写入口
// 带 If-Match + ?version=（整数令牌）；非整数/失配令牌平台返回 409（无 400 路径），
// 409 提示刷新重拉（不静默覆盖）；保存成功回填新令牌（否则下次拿旧令牌必然 409）。
// 平台另支持 updated_at ISO 令牌（If-Match / ?expected_updated_at=），前端当前只发
// 整数令牌是避免伪 409 的选择。契约见 utils/versionToken.js。

const stripImports = source => source.replace(/^import [\s\S]*? from ['"][^'"]+['"]\s*\n/gm, '')
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const copy = value => JSON.parse(JSON.stringify(value))
const messages = record => Object.fromEntries(
  ['success', 'warning', 'error', 'info'].map(kind => [kind, (...args) => record.push([kind, ...args])])
)

function apiSetup() {
  const calls = []
  const instance = {
    interceptors: { request: { use: () => {} }, response: { use: () => {} } },
    get: (...args) => { calls.push(['get', ...args]); return Promise.resolve({ data: {} }) },
    post: (...args) => { calls.push(['post', ...args]); return Promise.resolve({ data: {} }) },
    put: (...args) => { calls.push(['put', ...args]); return Promise.resolve({ data: {} }) },
    patch: (...args) => { calls.push(['patch', ...args]); return Promise.resolve({ data: {} }) },
    delete: (...args) => { calls.push(['delete', ...args]); return Promise.resolve({ data: {} }) },
  }
  const source = stripImports(read('../src/api/index.js'))
    .replace('export default', 'const __default =')
    .replace(/export const/g, 'const')
  const deps = {
    axios: { create: () => instance },
    normalizeError: () => {},
    window: {},
    versionRequestConfig,
  }
  const exposed = new Function(...Object.keys(deps), source + '\nreturn { api: __default }')(...Object.values(deps))
  return { ...exposed, calls }
}

function storeSetup(overrides = {}) {
  const calls = []
  const notices = []
  const api = {
    createTestCase: async payload => { calls.push(['create', copy(payload)]); return { data: { ...copy(payload), id: 42, version: 1, updated_at: 'T1' } } },
    updateTestCase: async (id, payload) => { calls.push(['update', id, copy(payload)]); return { data: { ...copy(payload), id, version: 2, updated_at: 'T2' } } },
    replaceCaseStandardSteps: async (id, steps, version) => { calls.push(['steps', id, copy(steps), copy(version)]); return { data: [{ id: 1, order: 1, action: 'wait', value: '1' }] } },
    getTestCase: async id => { calls.push(['getTestCase', id]); return { data: { id, version: 3, updated_at: 'T3' } } },
    getCaseStandardSteps: async () => ({ data: [] }),
    getTestCases: async () => ({ data: { items: [] } }),
    ...overrides,
  }
  const source = stripImports(read('../src/stores/useCaseStore.js')).replace('export const useCaseStore', 'const useCaseStore')
  let serial = 0
  const dependencies = {
    defineStore: (_name, factory) => factory, ref, computed, nextTick, api,
    ElMessage: messages(notices), createUuid: () => `step-${++serial}`,
    versionStateFrom, extractVersionState, isVersionConflict, VERSION_CONFLICT_MESSAGE,
  }
  const store = new Function(...Object.keys(dependencies), source + '\nreturn useCaseStore()')(...Object.values(dependencies))
  return { store, calls, notices, api }
}

function scenarioEditorSetup(overrides = {}) {
  const calls = []
  const notices = []
  const api = {
    getScenario: async id => { calls.push(['getScenario', id]); return { data: { id, name: '场景', version: 4, updated_at: 'S1' } } },
    getScenarioSteps: async () => ({ data: [] }),
    createScenario: async payload => { calls.push(['createScenario', copy(payload)]); return { data: { id: 73, version: 1, updated_at: 'S0' } } },
    updateScenario: async (id, payload) => { calls.push(['updateScenario', id, copy(payload)]); return { data: { id, name: payload.name, version: 5, updated_at: 'S2' } } },
    updateScenarioSteps: async (id, steps, version) => { calls.push(['steps', id, copy(steps), copy(version)]); return { data: { count: 1, version: 6, updated_at: 'S3' } } },
    ...overrides,
  }
  const source = stripImports(read('../src/views/scenarios/ScenarioEditor.vue')).match(/<script setup>([\s\S]*?)<\/script>/)[1]
  const route = { params: { ...(overrides.routeParams || {}) }, query: {} }
  const dependencies = {
    ref, computed, reactive, watch: () => {}, onMounted: () => {}, onUnmounted: () => {}, onActivated: () => {}, onDeactivated: () => {},
    nextTick: async () => {},
    useRoute: () => route, useRouter: () => ({ push: () => {} }), useUnsavedGuard: () => {},
    useUserStore: () => ({ userInfo: { id: 1, role: 'admin' } }), useClientMode: () => ({ isMobileMode: ref(false) }),
    ElMessage: messages(notices), ElMessageBox: { confirm: async () => {} }, createUuid: () => 'ui-step',
    setInterval: () => 1, clearInterval: () => {},
    describeRunSubmission: () => '', api,
    ...actionConstants, ...statusMeta,
    versionStateFrom, extractVersionState, isVersionConflict, VERSION_CONFLICT_MESSAGE,
  }
  const exports = 'saveSteps,scenarioVersion,currentScenario,scenarioSteps,scenarioId,savedSnapshot'
  const state = new Function(...Object.keys(dependencies), source + `\nreturn {${exports}}`)(...Object.values(dependencies))
  return { state, calls, notices }
}

test('version token prefers numeric version and falls back to updated_at', () => {
  assert.equal(versionTokenFrom({ version: 7, updated_at: 'T' }), '7')
  assert.equal(versionTokenFrom({ updated_at: '2026-10-26T10:00:00' }), '2026-10-26T10:00:00')
  assert.equal(versionTokenFrom({}), null)
  assert.equal(versionTokenFrom(null), null)
  assert.deepEqual(versionStateFrom({ id: 1, version: 7, updated_at: 'T' }), { version: 7, updated_at: 'T' })
  assert.deepEqual(versionStateFrom({ data: { version: 2 } }), { version: 2, updated_at: null })
  assert.deepEqual(versionStateFrom({ case: { updated_at: 'T9' } }), { version: null, updated_at: 'T9' })
  assert.equal(versionStateFrom([{ version: 1 }]), null, 'step rows carry no entity token')
})

test('request config sends If-Match plus version for integer tokens only (non-integer/mismatched tokens are 409, never 400)', () => {
  const numeric = versionRequestConfig({ version: 7, updated_at: '2026-10-26T10:00:00' })
  assert.equal(numeric.headers['If-Match'], '7')
  assert.deepEqual(numeric.params, { version: '7' })

  const isoOnly = versionRequestConfig({ version: null, updated_at: '2026-10-26T10:00:00' })
  assert.deepEqual(isoOnly, { headers: {}, params: {} }, 'platform takes ISO tokens via If-Match/?expected_updated_at=, but a mismatched token is a 409 (not 400) — frontend sends integer tokens only to avoid spurious 409s')

  const empty = versionRequestConfig(null)
  assert.deepEqual(empty, { headers: {}, params: {} }, 'missing token skips the check entirely')
})

test('extractVersionState backfills from response entity, X-Current-Version or ETag headers', () => {
  assert.deepEqual(extractVersionState({ data: { steps: [], version: 8, updated_at: 'T8' } }), { version: 8, updated_at: 'T8' })
  assert.deepEqual(extractVersionState({ data: [], headers: { 'X-Current-Version': '10' } }), { version: '10', updated_at: null }, 'platform version_lock.py回传头')
  assert.deepEqual(extractVersionState({ data: [{ id: 1 }], headers: { etag: '"9"' } }), { version: '9', updated_at: null })
  assert.deepEqual(extractVersionState({ data: [], headers: { ETag: 'W/"2026-10-26T11:00:00"' } }), { version: null, updated_at: '2026-10-26T11:00:00' })
  assert.deepEqual(extractVersionState({ data: [], headers: { 'x-version': '11', 'x-updated-at': 'TX' } }), { version: '11', updated_at: 'TX' })
  assert.equal(extractVersionState({ data: [{ id: 1 }] }), null)
})

test('api layer carries the token on both case and scenario step writes', async () => {
  const { api, calls } = apiSetup()
  await api.replaceCaseStandardSteps(5, [{ order: 1 }], { version: 7, updated_at: 'T' })
  await api.updateScenarioSteps(6, [{ case_id: 1, order: 1 }], { version: 3, updated_at: 'S1' })
  await api.replaceCaseStandardSteps(5, [], null)
  await api.updateScenarioSteps(6, [], { version: null, updated_at: 'S1' })
  const caseCall = calls.find(c => c[0] === 'put')
  assert.deepEqual([caseCall[1]], ['/cases/5/steps'])
  assert.equal(caseCall[3].headers['If-Match'], '7')
  assert.deepEqual(caseCall[3].params, { version: '7' })
  const scenarioCall = calls.find(c => c[0] === 'post')
  assert.deepEqual([scenarioCall[1]], ['/scenarios/6/steps'])
  assert.equal(scenarioCall[3].headers['If-Match'], '3')
  assert.deepEqual(scenarioCall[3].params, { version: '3' })
  const bare = calls.filter(c => c[0] === 'put')[1]
  assert.deepEqual(bare[3], { headers: {}, params: {} }, 'no token = no headers (legacy clients stay valid)')
  const bareScenario = calls.filter(c => c[0] === 'post')[1]
  assert.deepEqual(bareScenario[3], { headers: {}, params: {} }, 'non-integer token is never sent (a mismatched token would be a 409, not a 400)')
})

test('case save sends the loaded token on the steps write', async () => {
  const { store, calls } = storeSetup()
  store.currentCase.value = { id: 9, name: '并发用例', variables: [], steps: [] }
  store.caseVersion.value = { version: 2, updated_at: 'T2' }
  assert.equal(await store.saveCase(), true)
  const stepsCall = calls.find(c => c[0] === 'steps')
  assert.deepEqual(stepsCall[3], { version: 2, updated_at: 'T2' }, 'sends the pre-save token')
})

test('steps response token wins; bare-list response falls back to a re-pull', async () => {
  const wrapped = storeSetup({
    replaceCaseStandardSteps: async (id, steps, version) => ({ data: { steps: [{ id: 1, order: 1, action: 'wait', value: '1' }], version: 8, updated_at: 'T8' } }),
  })
  wrapped.store.currentCase.value = { id: 9, name: '用例', variables: [], steps: [] }
  wrapped.store.caseVersion.value = { version: 2, updated_at: 'T2' }
  assert.equal(await wrapped.store.saveCase(), true)
  assert.deepEqual(copy(wrapped.store.caseVersion.value), { version: 8, updated_at: 'T8' }, 'response entity token backfilled')

  const bare = storeSetup({
    replaceCaseStandardSteps: async () => ({ data: [{ id: 1, order: 1, action: 'wait', value: '1' }] }),
  })
  bare.store.currentCase.value = { id: 9, name: '用例', variables: [], steps: [] }
  bare.store.caseVersion.value = { version: 2, updated_at: 'T2' }
  assert.equal(await bare.store.saveCase(), true)
  assert.deepEqual(copy(bare.store.caseVersion.value), { version: 3, updated_at: 'T3' }, 'stale token replaced via getTestCase re-pull')
  assert.ok(bare.calls.some(c => c[0] === 'getTestCase'), 're-pull happened')
})

test('a concurrent steps write surfaces 409 as a refresh prompt and keeps the draft', async () => {
  const conflict = Object.assign(new Error('conflict'), { response: { status: 409, data: { detail: VERSION_CONFLICT_MESSAGE } } })
  const { store, notices } = storeSetup({ replaceCaseStandardSteps: async () => { throw conflict } })
  store.currentCase.value = { id: 9, name: '并发用例', variables: [], steps: [{ action: 'wait', value: '1' }] }
  store.caseVersion.value = { version: 2, updated_at: 'T2' }
  assert.equal(await store.saveCase(), false)
  assert.deepEqual(notices.map(([kind, text]) => [kind, text]), [['warning', VERSION_CONFLICT_MESSAGE]])
  assert.equal(store.hasUnsavedChanges.value, true, 'draft preserved, nothing silently overwritten')
  assert.equal(store.currentCase.value.steps.length, 1)
})

test('non-conflict failures keep the legacy retry message', async () => {
  const { store, notices } = storeSetup({ replaceCaseStandardSteps: async () => { throw new Error('steps offline') } })
  store.currentCase.value = { id: 9, name: '用例', variables: [], steps: [] }
  assert.equal(await store.saveCase(), false)
  assert.deepEqual(notices, [['error', '保存未完成，草稿已保留，请重试: steps offline']])
})

test('scenario save threads the entity token through name update and steps write, then backfills', async () => {
  const { state, calls } = scenarioEditorSetup({ routeParams: { id: '7' } })
  state.currentScenario.value.name = '场景草稿'
  state.scenarioSteps.value = [{ id: 2, name: '用例', alias: '别名' }]
  assert.equal(await state.saveSteps(), true)
  const stepsCall = calls.find(c => c[0] === 'steps')
  // 名称写入（version 5/S2）刷新令牌后才写步骤，且成功后回填步骤响应的 version 6/S3。
  assert.deepEqual(stepsCall[3], { version: 5, updated_at: 'S2' })
  assert.deepEqual(copy(state.scenarioVersion.value), { version: 6, updated_at: 'S3' })
})

test('scenario first save sends the create-response token to the steps write', async () => {
  const { state, calls } = scenarioEditorSetup()
  state.currentScenario.value.name = '新场景'
  state.scenarioSteps.value = [{ id: 2, name: '用例', alias: '别名' }]
  assert.equal(await state.saveSteps(), true)
  const stepsCall = calls.find(c => c[0] === 'steps')
  assert.deepEqual(stepsCall[3], { version: 1, updated_at: 'S0' })
  assert.deepEqual(copy(state.scenarioVersion.value), { version: 6, updated_at: 'S3' })
})

test('scenario steps 409 prompts a refresh instead of overwriting', async () => {
  const conflict = Object.assign(new Error('conflict'), { response: { status: 409, data: { detail: VERSION_CONFLICT_MESSAGE } } })
  const { state, notices } = scenarioEditorSetup({ updateScenarioSteps: async () => { throw conflict } })
  state.currentScenario.value.name = '场景草稿'
  state.scenarioSteps.value = [{ id: 2, name: '用例', alias: '别名' }]
  state.savedSnapshot.value = 'unsaved'
  assert.equal(await state.saveSteps(), false)
  assert.deepEqual(notices, [['warning', VERSION_CONFLICT_MESSAGE]])
  assert.equal(state.scenarioSteps.value.length, 1, 'draft steps preserved')
})

test('isVersionConflict only matches HTTP 409', () => {
  assert.equal(isVersionConflict({ response: { status: 409 } }), true)
  assert.equal(isVersionConflict({ response: { status: 500 } }), false)
  assert.equal(isVersionConflict(new Error('offline')), false)
  assert.equal(isVersionConflict(null), false)
})
