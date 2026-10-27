import assert from 'node:assert/strict'
import test from 'node:test'

import {
  POLL_REQUEST_TIMEOUT_MS,
  createGatedTask,
  createPollingLoop,
  isPageHidden,
  pollRequestConfig,
} from '../src/utils/polling.js'

const settle = async () => {
  for (let i = 0; i < 25; i++) await Promise.resolve()
}

const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

const fakeEventTarget = (initial = {}) => {
  const listeners = new Map()
  const target = {
    hidden: false,
    ...initial,
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, new Set())
      listeners.get(type).add(handler)
    },
    removeEventListener(type, handler) {
      listeners.get(type)?.delete(handler)
    },
    emit(type) {
      for (const handler of [...(listeners.get(type) || [])]) handler()
    },
    listenerCount(type) {
      return (listeners.get(type) || new Set()).size
    },
    ...initial,
  }
  return target
}

test('poll request config defaults to an explicit short timeout and stays overridable', () => {
  assert.equal(POLL_REQUEST_TIMEOUT_MS, 8000)
  assert.deepEqual(pollRequestConfig(), { timeout: 8000 })
  assert.deepEqual(pollRequestConfig({ timeout: 5000 }), { timeout: 5000 })
  assert.deepEqual(pollRequestConfig({ params: { a: 1 } }), { timeout: 8000, params: { a: 1 } })
})

test('createGatedTask skips calls while one is in flight and resumes afterwards', async () => {
  const gate = deferred()
  let calls = 0
  const task = createGatedTask(async () => {
    calls++
    return gate.promise
  })

  const first = task()
  const second = task()
  assert.equal(calls, 1, 'in-flight call is never doubled')
  assert.equal(await second, undefined, 'skipped call resolves with undefined')
  gate.resolve('ok')
  assert.equal(await first, 'ok')

  const third = task()
  assert.equal(calls, 2, 'next call runs after the previous settles')
  await third
})

test('isPageHidden is false without a document and reflects document.hidden otherwise', () => {
  assert.equal(isPageHidden(), false)
  const doc = fakeEventTarget({ hidden: true })
  const original = globalThis.document
  try {
    globalThis.document = doc
    assert.equal(isPageHidden(), true)
    doc.hidden = false
    assert.equal(isPageHidden(), false)
  } finally {
    if (original === undefined) delete globalThis.document
    else globalThis.document = original
  }
})

test('polling loop never overlaps ticks and re-arms only after completion', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const doc = fakeEventTarget()
  const win = fakeEventTarget()
  const gates = []
  let runs = 0
  const loop = createPollingLoop({
    intervalMs: 1000,
    getDocument: () => doc,
    getWindow: () => win,
    tick: () => {
      runs++
      const gate = deferred()
      gates.push(gate)
      return gate.promise
    },
    onError: () => {},
  })

  loop.start()
  t.mock.timers.tick(1000)
  await settle()
  assert.equal(runs, 1)

  // 上一轮仍在途时，间隔到点也不再出票（在途不重叠）
  t.mock.timers.tick(5000)
  await settle()
  assert.equal(runs, 1)
  assert.equal(loop.isInflight(), true)

  gates[0].resolve()
  await settle()
  assert.equal(loop.isInflight(), false)

  // 完成后 setTimeout 续排：从完成时刻再等一个间隔
  t.mock.timers.tick(999)
  await settle()
  assert.equal(runs, 1)
  t.mock.timers.tick(1)
  await settle()
  assert.equal(runs, 2)

  gates[1].resolve()
  await settle()
  loop.stop()
  t.mock.timers.tick(10000)
  await settle()
  assert.equal(runs, 2, 'stopped loop never ticks again')
})

test('polling loop pauses while the page is hidden and resumes when visible', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const doc = fakeEventTarget({ hidden: true })
  const win = fakeEventTarget()
  let runs = 0
  const loop = createPollingLoop({
    intervalMs: 1000,
    immediate: true,
    getDocument: () => doc,
    getWindow: () => win,
    tick: () => {
      runs++
    },
    onError: () => {},
  })

  loop.start()
  t.mock.timers.tick(5000)
  await settle()
  assert.equal(runs, 0, 'hidden page never polls (start defers, ticks are gated)')

  doc.hidden = false
  doc.emit('visibilitychange')
  t.mock.timers.tick(1000)
  await settle()
  assert.equal(runs, 1, 'visible again resumes polling')

  doc.hidden = true
  doc.emit('visibilitychange')
  t.mock.timers.tick(10000)
  await settle()
  assert.equal(runs, 1, 'hidden again pauses the cadence')

  doc.hidden = false
  doc.emit('visibilitychange')
  t.mock.timers.tick(1000)
  await settle()
  assert.equal(runs, 2)
  loop.stop()
})

test('polling loop pauses on pagehide and resumes on pageshow', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const doc = fakeEventTarget()
  const win = fakeEventTarget()
  let runs = 0
  const loop = createPollingLoop({
    intervalMs: 1000,
    getDocument: () => doc,
    getWindow: () => win,
    tick: () => {
      runs++
    },
    onError: () => {},
  })

  loop.start()
  t.mock.timers.tick(1000)
  await settle()
  assert.equal(runs, 1)

  win.emit('pagehide')
  t.mock.timers.tick(10000)
  await settle()
  assert.equal(runs, 1, 'pagehide stops the cadence')

  win.emit('pageshow')
  t.mock.timers.tick(1000)
  await settle()
  assert.equal(runs, 2, 'pageshow resumes the cadence')
  loop.stop()
})

test('polling loop survives tick errors and keeps its cadence', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const doc = fakeEventTarget()
  const win = fakeEventTarget()
  const errors = []
  let runs = 0
  const loop = createPollingLoop({
    intervalMs: 1000,
    getDocument: () => doc,
    getWindow: () => win,
    tick: () => {
      runs++
      throw new Error(`boom-${runs}`)
    },
    onError: (err) => errors.push(String(err?.message || err)),
  })

  loop.start()
  t.mock.timers.tick(1000)
  await settle()
  t.mock.timers.tick(1000)
  await settle()
  assert.equal(runs, 2)
  assert.deepEqual(errors, ['boom-1', 'boom-2'])
  loop.stop()
})

test('polling loop skips the fetch when disabled but keeps ticking, and adapts intervals', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const doc = fakeEventTarget()
  const win = fakeEventTarget()
  let enabled = false
  let runs = 0
  const intervals = [500, 2500]
  const loop = createPollingLoop({
    intervalMs: () => intervals.shift() ?? 1000,
    getDocument: () => doc,
    getWindow: () => win,
    isEnabled: () => enabled,
    tick: () => {
      runs++
    },
    onError: () => {},
  })

  loop.start()
  t.mock.timers.tick(500)
  await settle()
  assert.equal(runs, 0, 'disabled tick fetches nothing')

  enabled = true
  t.mock.timers.tick(2500)
  await settle()
  assert.equal(runs, 1, 'enabled tick fetches, using the adaptive interval')
  loop.stop()
})

test('stop() detaches visibility listeners so a dead poller cannot be revived', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const doc = fakeEventTarget()
  const win = fakeEventTarget()
  let runs = 0
  const loop = createPollingLoop({
    intervalMs: 1000,
    getDocument: () => doc,
    getWindow: () => win,
    tick: () => {
      runs++
    },
    onError: () => {},
  })

  loop.start()
  assert.equal(doc.listenerCount('visibilitychange'), 1)
  assert.equal(win.listenerCount('pagehide'), 1)
  loop.stop()
  assert.equal(doc.listenerCount('visibilitychange'), 0)
  assert.equal(win.listenerCount('pagehide'), 0)
  doc.emit('visibilitychange')
  win.emit('pageshow')
  t.mock.timers.tick(10000)
  await settle()
  assert.equal(runs, 0)
})
