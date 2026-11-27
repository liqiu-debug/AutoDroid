import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'

import {
  formatLocatorTitle,
  isValidCompositeSelector,
  parseCompositeConditions,
  parseCompositeObject,
  serializeCompositeConditions,
} from '../src/utils/compositeLocator.js'
import { parseHierarchy } from '../src/utils/hierarchy.js'
import {
  evaluateRecordableNode,
  findBestRecordableNode,
  getOrBuildNodeIndex,
} from '../src/utils/recordableNode.js'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const BACKEND_EXPORT_DIR = path.resolve(__dirname, '../../backend/autojs_export')

class MiniDomParser {
  parseFromString(xml) {
    const root = { tagName: 'root', children: [], attrs: {} }
    const stack = [root]
    const tagRe = /<\/?([A-Za-z0-9_.:-]+)([^>]*)>/g
    let match
    while ((match = tagRe.exec(xml))) {
      const full = match[0]
      const tagName = match[1]
      const attrText = match[2] || ''
      if (full.startsWith('</')) {
        if (stack.length > 1) stack.pop()
        continue
      }
      const attrs = {}
      const attrRe = /([A-Za-z0-9_.:-]+)="([^"]*)"/g
      let attrMatch
      while ((attrMatch = attrRe.exec(attrText))) {
        attrs[attrMatch[1]] = attrMatch[2]
      }
      const node = {
        nodeType: 1,
        nodeName: tagName,
        tagName,
        attrs,
        children: [],
        get childNodes() {
          return this.children
        },
        getAttribute(name) {
          return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null
        },
      }
      stack[stack.length - 1].children.push(node)
      const selfClosing = attrText.trim().endsWith('/')
      if (!selfClosing) {
        stack.push(node)
      }
    }
    const documentElement = root.children[0] || root
    return { documentElement }
  }
}

globalThis.DOMParser = MiniDomParser

test('compositeLocator utils parse, serialize, validate and format cleanly', () => {
  const obj = parseCompositeObject('{"text":"立即购买","resourceId":"com.demo:id/buy_btn"}')
  assert.deepEqual(obj, { id: 'com.demo:id/buy_btn', text: '立即购买' })

  const rows = parseCompositeConditions('{"text":"提交","className":"android.widget.Button"}')
  assert.deepEqual(rows, [
    { key: 'text', value: '提交' },
    { key: 'class_name', value: 'android.widget.Button' },
  ])

  const serialized = serializeCompositeConditions([
    { key: 'text', value: '确定' },
    { key: 'id', value: 'com.demo:id/ok' },
  ])
  assert.equal(serialized, '{"id":"com.demo:id/ok","text":"确定"}')
  assert.equal(isValidCompositeSelector(serialized), true)
  assert.equal(isValidCompositeSelector('{bad-json'), false)
  assert.equal(isValidCompositeSelector('{}'), false)

  const title = formatLocatorTitle('composite', serialized)
  assert.ok(title.includes('ok'))
  assert.ok(title.includes('确定'))
})

test('recordableNode synthesizes unique id/composite/class_name and caches WeakMap index', () => {
  const xml = `
    <hierarchy rotation="0">
      <node index="0" text="" resource-id="android:id/content" class="android.widget.FrameLayout" content-desc="" clickable="false" bounds="[0,0][1080,1920]">
        <node index="0" text="" resource-id="com.demo:id/pure_icon" class="android.widget.ImageView" content-desc="" clickable="true" bounds="[40,80][140,180]" />
        <node index="1" text="微信" resource-id="com.demo:id/pay_item" class="android.widget.TextView" content-desc="" clickable="true" bounds="[40,300][500,400]" />
        <node index="2" text="支付宝" resource-id="com.demo:id/pay_item" class="android.widget.TextView" content-desc="" clickable="true" bounds="[40,420][500,520]" />
        <node index="3" text="" resource-id="com.demo:id/dup_icon" class="android.widget.ImageView" content-desc="" clickable="true" bounds="[40,600][140,700]" />
        <node index="4" text="" resource-id="com.demo:id/dup_icon" class="android.widget.ImageView" content-desc="" clickable="true" bounds="[40,750][140,850]" />
        <node index="5" text="" resource-id="" class="android.widget.EditText" content-desc="" clickable="true" bounds="[40,950][900,1050]" />
        <node index="6" text="" resource-id="" class="android.widget.FrameLayout" content-desc="" clickable="true" bounds="[40,1150][900,1250]" />
      </node>
    </hierarchy>
  `
  const nodes = parseHierarchy(xml)
  assert.ok(nodes.every((n) => typeof n.enter === 'number' && typeof n.exit === 'number'))

  // WeakMap 同引用缓存复用验证
  const idx1 = getOrBuildNodeIndex(nodes)
  const idx2 = getOrBuildNodeIndex(nodes)
  assert.equal(idx1, idx2)

  // 默认开关全关：纯图标与纯 EditText 无语义定位
  const defaultIcon = findBestRecordableNode(nodes, 90, 130)
  assert.equal(defaultIcon, null)

  // 开启 resource_id：纯图标命中 resourceId，相同 id 不同 text 合成 composite，重复纯图标返回 null
  const idCfg = {
    enable_resource_id: true,
    enable_class_name: false,
    priority: 'semantic_first',
  }
  const iconEval = evaluateRecordableNode(nodes, 90, 130, idCfg)
  assert.ok(iconEval)
  assert.equal(iconEval.strategy, 'resourceId')
  assert.equal(iconEval.selector, 'com.demo:id/pure_icon')
  assert.equal(findBestRecordableNode(nodes, 90, 130, idCfg), iconEval.node)

  const alipayEval = evaluateRecordableNode(nodes, 200, 460, {
    ...idCfg,
    priority: 'id_first',
  })
  assert.ok(alipayEval)
  assert.equal(alipayEval.strategy, 'composite')
  assert.equal(alipayEval.selector, '{"id":"com.demo:id/pay_item","text":"支付宝"}')

  // 重复纯图标即使命中坐标，也不可记录为唯一元素
  assert.equal(findBestRecordableNode(nodes, 90, 650, idCfg), null)
  assert.equal(evaluateRecordableNode(nodes, 90, 650, idCfg), null)

  // 开启 class_name：唯一 EditText 可录制；通用 FrameLayout 过滤不录
  const clsCfg = {
    enable_resource_id: false,
    enable_class_name: true,
    priority: 'semantic_first',
  }
  const editEval = evaluateRecordableNode(nodes, 200, 1000, clsCfg)
  assert.ok(editEval)
  assert.equal(editEval.strategy, 'className')
  assert.equal(editEval.selector, 'android.widget.EditText')
  assert.equal(findBestRecordableNode(nodes, 200, 1200, clsCfg), null)
})

test('autojs runtime.js executes composite selector chains and textContains fallback', () => {
  const runtimeSrc = fs.readFileSync(path.join(BACKEND_EXPORT_DIR, 'runtime.js'), 'utf8')
  const buildChain = (recordedCalls, initial) => {
    const chain = [initial]
    const makeSelector = () => ({
      id(v) {
        chain.push({ method: 'id', value: v })
        return makeSelector()
      },
      text(v) {
        chain.push({ method: 'text', value: v })
        return makeSelector()
      },
      textContains(v) {
        chain.push({ method: 'textContains', value: v })
        return makeSelector()
      },
      desc(v) {
        chain.push({ method: 'desc', value: v })
        return makeSelector()
      },
      descContains(v) {
        chain.push({ method: 'descContains', value: v })
        return makeSelector()
      },
      className(v) {
        chain.push({ method: 'className', value: v })
        return makeSelector()
      },
      findOne(timeout) {
        recordedCalls.push({ chain: [...chain], timeout })
        const hasExactText = chain.some((c) => c.method === 'text')
        if (hasExactText) return null
        return {
          clickable: () => true,
          click: () => true,
          bounds: () => ({ centerX: () => 100, centerY: () => 200 }),
        }
      },
    })
    return makeSelector()
  }

  const recordedCalls = []
  const sandbox = {
    console,
    JSON,
    Object,
    Array,
    String,
    Number,
    Boolean,
    RegExp,
    Error,
    Date,
    Math,
    automator: { click: () => true },
    device: { width: 1080, height: 1920 },
    images: {},
    threads: {
      start(fn) {
        fn()
        return { interrupt() {} }
      },
    },
    files: {
      cwd: () => '.',
      join: (...parts) => parts.join('/'),
      exists: () => false,
      read: () => '{}',
    },
    sleep() {},
    toast() {},
    click() {},
    swipe() {},
    back() {},
    home() {},
    setClip() {},
    app: { launch() {} },
    module: { exports: {} },
    exports: {},
    id(v) {
      return buildChain(recordedCalls, { method: 'id', value: v })
    },
    text(v) {
      return buildChain(recordedCalls, { method: 'text', value: v })
    },
    textContains(v) {
      return buildChain(recordedCalls, { method: 'textContains', value: v })
    },
    desc(v) {
      return buildChain(recordedCalls, { method: 'desc', value: v })
    },
    descContains(v) {
      return buildChain(recordedCalls, { method: 'descContains', value: v })
    },
    className(v) {
      return buildChain(recordedCalls, { method: 'className', value: v })
    },
  }
  sandbox.global = sandbox
  vm.runInNewContext(runtimeSrc, sandbox)

  const runtimeApi = sandbox.module.exports
  const ctx = runtimeApi.createContext({ cases: [] }, '.', { platform: 'android' })
  const res = runtimeApi.runCase(ctx, {
    case_id: 1,
    name: 'composite-test',
    variables: {},
    steps: [
      {
        order: 1,
        action: 'click',
        locators: [
          {
            by: 'composite',
            selector: '{"id":"com.demo:id/pay","text":"立即购买"}',
          },
        ],
      },
    ],
  })
  assert.equal(res.success, true, JSON.stringify(res.steps[0]))
  assert.equal(res.steps[0].status, 'PASS', JSON.stringify(res.steps[0]))
  assert.equal(recordedCalls.length, 2)
  assert.deepEqual(recordedCalls[0].chain, [
    { method: 'id', value: 'com.demo:id/pay' },
    { method: 'text', value: '立即购买' },
  ])
  assert.deepEqual(recordedCalls[1].chain, [
    { method: 'id', value: 'com.demo:id/pay' },
    { method: 'textContains', value: '立即购买' },
  ])
})

test('ios_adapter.js supports chainable id/text/textContains/desc/className predicates', () => {
  const adapterSrc = fs.readFileSync(path.join(BACKEND_EXPORT_DIR, 'adapters/ios_adapter.js'), 'utf8')
  const sandbox = {
    console,
    JSON,
    Object,
    Array,
    String,
    Number,
    Boolean,
    RegExp,
    Error,
    Date,
    Math,
    __adt: {
      log() {},
      sleep() {},
      findElements() {
        return ['e1']
      },
      deviceInfo() {
        return { width: 390, height: 844 }
      },
    },
    device: { width: 390, height: 844 },
    sleep() {},
    module: { exports: {} },
    global: {},
  }
  sandbox.global = sandbox
  vm.runInNewContext(adapterSrc, sandbox)

  assert.equal(typeof sandbox.id, 'function')
  const sel = sandbox.id('pay_btn').text('立即购买').className('XCUIElementTypeButton')
  assert.ok(sel)
  assert.equal(typeof sel.textContains, 'function')
  assert.equal(typeof sel.desc, 'function')
  assert.equal(sel.__spec.kind, 'predicate')
  assert.ok(Array.isArray(sel.__spec.clauses))
  assert.ok(sel.__spec.clauses.some((c) => c.includes("name == 'pay_btn'")))
  assert.ok(sel.__spec.clauses.some((c) => c.includes("label == '立即购买'")))
  assert.ok(sel.__spec.clauses.some((c) => c.includes("type == 'XCUIElementTypeButton'")))
})
