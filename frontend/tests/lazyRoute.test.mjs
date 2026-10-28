/**
 * lazyRoute：路由懒加载韧性包装的行为测试。
 *
 * 背景：「长时间不操作后点击无响应」的真身 = 动态 import chunk 失败后
 * 浏览器把失败缓存进模块表（此后同 chunk 每次点击都瞬间失败），且路由
 * 错误被静默吞掉。对策：瞬时失败重试；终态失败自动重载拉新壳（30s 内
 * 只自动刷一次防重载环）；不自动刷时给出可见提示。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { lazyRoute, isModuleFetchFailure, setLazyRouteErrorHandler } from '../src/utils/lazyRoute.js'

const moduleError = () => new Error('Failed to fetch dynamically imported module: https://x/assets/Page-abc.js')

function fakeStorage(initial = {}) {
  const map = new Map(Object.entries(initial))
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
  }
}

test('isModuleFetchFailure 识别动态 import 失败', () => {
  assert.equal(isModuleFetchFailure(moduleError()), true)
  assert.equal(isModuleFetchFailure(new Error('boom')), false)
})

test('瞬时失败自动重试，第三次成功', async () => {
  let calls = 0
  const loader = async () => {
    calls += 1
    if (calls < 3) throw moduleError()
    return { default: 'PageComponent' }
  }
  const storage = fakeStorage()
  let reloaded = 0
  const run = lazyRoute(loader, { storage, reload: () => { reloaded += 1 } })
  const result = await run()
  assert.equal(result.default, 'PageComponent')
  assert.equal(calls, 3, '失败两次后第三次成功')
  assert.equal(reloaded, 0)
})

test('非模块加载错误不重试直接抛', async () => {
  let calls = 0
  const loader = async () => {
    calls += 1
    throw new Error('component render bug')
  }
  const run = lazyRoute(loader, { storage: fakeStorage(), reload: () => {} })
  await assert.rejects(run(), /component render bug/)
  assert.equal(calls, 1)
})

test('终态失败：自动重载一次且不重复刷（防重载环）', async () => {
  const storage = fakeStorage()
  let reloaded = 0
  const loader = async () => { throw moduleError() }
  const run = lazyRoute(loader, { storage, reload: () => { reloaded += 1 }, now: () => 1000 })

  const pending = run()
  await new Promise((r) => setTimeout(r, 50))
  assert.equal(reloaded, 1, '首次终态失败自动重载')

  // 30s 窗口内再次失败：不自动刷，转为可见提示 + 抛出
  const errors = []
  const run2 = lazyRoute(loader, {
    storage, reload: () => { reloaded += 1 }, now: () => 1000 + 5000,
    onError: (message) => errors.push(message),
  })
  await assert.rejects(run2())
  assert.equal(reloaded, 1, '重载窗口内不得循环刷新')
  assert.equal(errors.length, 1, '转为可见错误提示')
  assert.match(errors[0], /加载失败/)
})

test('成功加载清除重载标记（下次失败可再自动重载）', async () => {
  const storage = fakeStorage({ 'autodroid:chunk-reload-at': '1000' })
  let calls = 0
  const loader = async () => {
    calls += 1
    if (calls === 1) throw moduleError()
    return { default: 'OK' }
  }
  const run = lazyRoute(loader, { storage, reload: () => {}, now: () => 2000 })
  await run()
  assert.equal(storage.getItem('autodroid:chunk-reload-at'), null, '成功后清除重载标记')
})

test('setLazyRouteErrorHandler 注入可见提示', async () => {
  const seen = []
  setLazyRouteErrorHandler((message) => seen.push(message))
  const loader = async () => { throw moduleError() }
  const storage = fakeStorage({ 'autodroid:chunk-reload-at': '1' })
  const run = lazyRoute(loader, { storage, reload: () => {}, now: () => 2000 })
  await assert.rejects(run())
  assert.equal(seen.length, 1)
  setLazyRouteErrorHandler(null)
})

// ---- 源码契约：路由懒加载全部经 lazyRoute 包装 + onError 兜底 ----
import { readFileSync } from 'node:fs'

test('router 全部懒加载点已包装且注册 onError 兜底', () => {
  const routerSrc = readFileSync(new URL('../src/router/index.js', import.meta.url), 'utf8')
  const bare = routerSrc.match(/component:\s*\(\)\s*=>\s*import\(/g) || []
  assert.equal(bare.length, 0, '不允许裸动态 import（须经 lazyRoute 包装）')
  assert.ok(routerSrc.includes('lazyRoute(() => import('), '存在 lazyRoute 包装')
  assert.ok(routerSrc.includes('router.onError('), '注册路由级错误兜底')
  assert.ok(routerSrc.includes('setLazyRouteErrorHandler('), '注入可见错误提示')
})
