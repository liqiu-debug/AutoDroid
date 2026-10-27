import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { ref } from 'vue'

// 与 apiDebug.test.mjs 相同：只替换模块依赖，composable 逻辑原样执行。
const source = readFileSync(new URL('../src/composables/useResizableAside.js', import.meta.url), 'utf8')
  .replace(/^import .*\n/gm, '')
  .replace(/^export (const|function) /gm, '$1 ')
const loadModule = new Function('ref', 'onBeforeUnmount',
  `${source}\nreturn { clampAsideWidth, readAsideWidth, writeAsideWidth, useResizableAside, ASIDE_WIDTH_STORAGE_KEY, ASIDE_DEFAULT_WIDTH, ASIDE_MIN_WIDTH, ASIDE_MAX_WIDTH };`)

function fakeStorage(initial = {}) {
  const map = new Map(Object.entries(initial))
  return {
    map,
    getItem: key => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => { map.set(key, String(value)) },
  }
}

function fakeDocument() {
  const listeners = new Map()
  return {
    listeners,
    body: { style: { userSelect: '', cursor: '' } },
    addEventListener(type, fn) { listeners.set(type, [...(listeners.get(type) || []), fn]) },
    removeEventListener(type, fn) { listeners.set(type, (listeners.get(type) || []).filter(item => item !== fn)) },
    fire(type, event = {}) { for (const fn of [...(listeners.get(type) || [])]) fn(event) },
    count(type) { return (listeners.get(type) || []).length },
  }
}

function setup(options = {}) {
  const cleanups = []
  const module = loadModule(ref, fn => cleanups.push(fn))
  const state = module.useResizableAside({ storage: fakeStorage(), document: fakeDocument(), ...options })
  return { module, state, cleanups, unmount: () => cleanups.forEach(fn => fn()) }
}

test('目录宽度默认 260，越界与非法值收敛到 200–480', () => {
  const module = loadModule(ref, () => {})
  assert.equal(module.ASIDE_DEFAULT_WIDTH, 260)
  assert.equal(module.ASIDE_MIN_WIDTH, 200)
  assert.equal(module.ASIDE_MAX_WIDTH, 480)
  assert.equal(module.clampAsideWidth(260), 260)
  assert.equal(module.clampAsideWidth(305.4), 305)
  assert.equal(module.clampAsideWidth(5000), 480)
  assert.equal(module.clampAsideWidth(120), 200)
  assert.equal(module.clampAsideWidth('abc'), 260)
  const empty = fakeStorage()
  assert.equal(module.readAsideWidth(empty), 260)
  assert.equal(module.readAsideWidth(fakeStorage({ [module.ASIDE_WIDTH_STORAGE_KEY]: '5000' })), 480)
  assert.equal(module.readAsideWidth(fakeStorage({ [module.ASIDE_WIDTH_STORAGE_KEY]: '' })), 260)
})

test('宽度持久化到 localStorage，重启（新实例）后保留', () => {
  const storage = fakeStorage()
  const first = loadModule(ref, () => {})
  const a = first.useResizableAside({ storage, document: fakeDocument() })
  assert.equal(a.width.value, 260)
  a.width.value = 333
  first.writeAsideWidth(storage, first.ASIDE_WIDTH_STORAGE_KEY, a.width.value)
  assert.equal(storage.map.get(first.ASIDE_WIDTH_STORAGE_KEY), '333')
  const second = loadModule(ref, () => {})
  const b = second.useResizableAside({ storage, document: fakeDocument() })
  assert.equal(b.width.value, 333)
})

test('拖拽把手左右调整宽度并在松手后持久化，拖动期间禁止选中', () => {
  const storage = fakeStorage()
  const doc = fakeDocument()
  const { state } = setup({ storage, document: doc })
  let prevented = 0
  state.startResize({ button: 0, clientX: 100, preventDefault: () => { prevented += 1 } })
  assert.equal(prevented, 1)
  assert.equal(doc.count('mousemove'), 1)
  assert.equal(doc.body.style.userSelect, 'none')
  assert.equal(doc.body.style.cursor, 'col-resize')
  doc.fire('mousemove', { clientX: 200 })
  assert.equal(state.width.value, 360)
  assert.equal(storage.map.get('autodroid.folder_aside_width'), undefined)
  doc.fire('mousemove', { clientX: -2000 })
  assert.equal(state.width.value, 200)
  doc.fire('mousemove', { clientX: 130 })
  assert.equal(state.width.value, 290)
  doc.fire('mouseup')
  assert.equal(doc.count('mousemove'), 0)
  assert.equal(doc.count('mouseup'), 0)
  assert.equal(doc.body.style.userSelect, '')
  assert.equal(storage.map.get('autodroid.folder_aside_width'), '290')
})

test('宽度上限 480，非左键不启动拖拽', () => {
  const doc = fakeDocument()
  const { state } = setup({ document: doc })
  state.startResize({ button: 0, clientX: 0, preventDefault: () => {} })
  doc.fire('mousemove', { clientX: 900 })
  assert.equal(state.width.value, 480)
  doc.fire('mouseup')
  state.startResize({ button: 2, clientX: 0, preventDefault: () => {} })
  assert.equal(doc.count('mousemove'), 0)
})

test('拖拽中离开页面会清理监听并落盘当前宽度', () => {
  const storage = fakeStorage()
  const doc = fakeDocument()
  const { state, unmount } = setup({ storage, document: doc })
  state.startResize({ button: 0, clientX: 0, preventDefault: () => {} })
  doc.fire('mousemove', { clientX: 40 })
  unmount()
  assert.equal(doc.count('mousemove'), 0)
  assert.equal(doc.count('mouseup'), 0)
  assert.equal(doc.body.style.userSelect, '')
  assert.equal(storage.map.get('autodroid.folder_aside_width'), '300')
})
