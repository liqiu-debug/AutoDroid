import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { ref, computed, nextTick } from 'vue'
import * as versionToken from '../src/utils/versionToken.js'
import * as imageOptions from '../src/utils/imageOptions.js'

const stripImports = source => source.replace(/^import [\s\S]*? from ['"][^'"]+['"]\s*\n/gm, '')

function createStore() {
  const source = stripImports(readFileSync(new URL('../src/stores/useCaseStore.js', import.meta.url), 'utf8')).replace('export const useCaseStore', 'const useCaseStore')
  let seq = 0
  const api = {
    createTestCase: async payload => ({ data: { ...payload, id: 1 } }),
    updateTestCase: async (id, payload) => ({ data: { ...payload, id } }),
    replaceCaseStandardSteps: async () => ({ data: undefined }),
    getTestCases: async () => ({ data: { items: [] } }),
  }
  const deps = {
    defineStore: (_name, factory) => factory,
    ref,
    computed,
    nextTick,
    api,
    ElMessage: Object.fromEntries(['success', 'warning', 'error', 'info'].map(k => [k, () => {}])),
    createUuid: () => `uuid-${++seq}`,
    ...versionToken,
    ...imageOptions,
  }
  return new Function(...Object.keys(deps), source + '\nreturn useCaseStore()')(...Object.values(deps))
}

test('useCaseStore preserves disabled state in save payload without polluting enabled steps', async () => {
  const calls = []
  const source = stripImports(readFileSync(new URL('../src/stores/useCaseStore.js', import.meta.url), 'utf8')).replace('export const useCaseStore', 'const useCaseStore')
  let seq = 0
  const api = {
    createTestCase: async payload => { calls.push(['legacy', payload]); return { data: { ...payload, id: 10 } } },
    updateTestCase: async (id, payload) => { calls.push(['legacy', payload]); return { data: { ...payload, id } } },
    replaceCaseStandardSteps: async (id, steps) => { calls.push(['std', steps]); return { data: undefined } },
    getTestCases: async () => ({ data: { items: [] } }),
  }
  const deps = {
    defineStore: (_name, factory) => factory,
    ref,
    computed,
    nextTick,
    api,
    ElMessage: Object.fromEntries(['success', 'warning', 'error', 'info'].map(k => [k, () => {}])),
    createUuid: () => `uuid-${++seq}`,
    ...versionToken,
    ...imageOptions,
  }
  const store = new Function(...Object.keys(deps), source + '\nreturn useCaseStore()')(...Object.values(deps))
  store.newCase()
  store.currentCase.value.name = '步骤测试'
  store.addStep({ action: 'back' })
  store.addStep({ action: 'home', disabled: true })

  assert.equal(await store.saveCase(), true)
  const legacyCall = calls.find(c => c[0] === 'legacy')[1]
  const stdCall = calls.find(c => c[0] === 'std')[1]
  assert.equal('disabled' in legacyCall.steps[0], false)
  assert.equal(legacyCall.steps[1].disabled, true)
  assert.equal('disabled' in stdCall[0].args, false)
  assert.equal(stdCall[1].args.disabled, true)
})

test('StepBuilder and LogConsole expose duplicate, disable toggle, and start_step_index wiring', () => {
  const sb = readFileSync(new URL('../src/components/StepBuilder.vue', import.meta.url), 'utf8')
  const lc = readFileSync(new URL('../src/components/LogConsole.vue', import.meta.url), 'utf8')
  const ce = readFileSync(new URL('../src/views/cases/CaseEditor.vue', import.meta.url), 'utf8')

  assert.match(sb, /duplicateStep/)
  assert.match(sb, /toggleStepDisabled/)
  assert.match(sb, /run-from-step/)
  assert.match(sb, /已禁用/)
  assert.match(lc, /start_step_index=/)
  assert.match(ce, /@run-from-step=/)
})

test('StepBuilder and ScenarioEditor stretch drop zones across trailing whitespace and keep empty states non-blocking', () => {
  const sb = readFileSync(new URL('../src/components/StepBuilder.vue', import.meta.url), 'utf8')
  const se = readFileSync(new URL('../src/views/scenarios/ScenarioEditor.vue', import.meta.url), 'utf8')

  assert.match(sb, /\.draggable-container\s*\{[\s\S]*?min-height:\s*100%;/)
  assert.match(sb, /\.step-list-empty\s*\{[\s\S]*?pointer-events:\s*none;/)
  assert.match(se, /\.step-draggable\s*\{[\s\S]*?min-height:\s*100%;/)
  assert.match(se, /\.step-empty-overlay\s*\{[\s\S]*?pointer-events:\s*none;/)
})
