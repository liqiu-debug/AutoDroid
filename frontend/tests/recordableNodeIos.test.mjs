import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { parseHierarchy } from '../src/utils/hierarchy.js'
import { findBestNodeAtPoint, findBestRecordableNode } from '../src/utils/recordableNode.js'

// iOS 无障碍树里的布局容器也带 label / name：点在这些区域要走图像点击（参考 Android），
// 不能录成「点击 工具栏」。节点取自 2026-11-08 真机（iPhone 14 Pro / iOS 27.3）。

/** 够用的 XML → DOM 替身（仓库无 DOM 测试库；parseHierarchy 只用到这几个接口）。 */
class MiniDomParser {
  parseFromString(xml) {
    const root = { childNodes: [] }
    const stack = [root]
    for (const [, close, name, attrs, selfClose] of xml.matchAll(/<(\/?)([\w:]+)((?:\s+[\w-]+="[^"]*")*)\s*(\/?)>/g)) {
      if (close) { stack.pop(); continue }
      const map = Object.fromEntries([...attrs.matchAll(/([\w-]+)="([^"]*)"/g)].map(m => [m[1], m[2]]))
      const el = { nodeType: 1, nodeName: name, childNodes: [], getAttribute: key => map[key] ?? null }
      stack.at(-1).childNodes.push(el)
      if (!selfClose) stack.push(el)
    }
    return { documentElement: root.childNodes[0] }
  }
}
globalThis.DOMParser = MiniDomParser

const el = (type, attrs, children = '') => {
  const body = Object.entries({ type: `XCUIElementType${type}`, ...attrs }).map(([k, v]) => `${k}="${v}"`).join(' ')
  return `<XCUIElementType${type} ${body}>${children}</XCUIElementType${type}>`
}
const rect = (x, y, width, height) => ({ x, y, width, height })
const page = (...children) => el('Application', { name: '备忘录', label: '备忘录', ...rect(0, 0, 393, 852) },
  el('Window', rect(0, 0, 393, 852), children.join('')))
const nodesOf = (xml) => parseHierarchy(xml, { platform: 'ios', scale: 3 })
const px = point => point * 3

const NOTES = page(
  el('Toolbar', { name: 'Toolbar', label: '工具栏', ...rect(0, 0, 393, 852) }, el('Other', rect(0, 760, 393, 90))),
  el('CollectionView', { name: '我的备忘录', label: '我的备忘录', ...rect(0, 0, 393, 852) },
    el('Cell', { name: '飞书机器人功能清单', label: '飞书机器人功能清单', ...rect(16, 236, 361, 81) }, el('StaticText', { label: '飞书机器人功能清单', ...rect(32, 246, 300, 20) }))
      + el('Other', { name: '垂直滚动条, 4页', label: '垂直滚动条, 4页', ...rect(360, 113, 30, 684) }, el('Other', rect(370, 200, 10, 100)))),
  el('NavigationBar', { name: 'iCloud全部备忘录', ...rect(0, 59, 393, 133) }, el('Button', { name: 'BackButton', label: '返回', ...rect(16, 70, 44, 44) })),
  el('Other', { name: 'HomeView', ...rect(8, 476, 377, 368) }, el('Other', rect(8, 476, 377, 100)))
)

test('blank areas never record a layout container', () => {
  const nodes = nodesOf(NOTES)
  for (const [x, y, where] of [[100, 400, '工具栏 + 列表'], [385, 300, '滚动条'], [200, 120, '导航栏空白'], [200, 650, '只有标识的容器']]) {
    assert.equal(findBestRecordableNode(nodes, px(x), px(y)), null, where)
  }
})

test('blank areas always offer the image-click prompt with a small selection around the tap', () => {
  const nodes = nodesOf(NOTES)
  const fallback = findBestNodeAtPoint(nodes, px(100), px(400))
  assert.ok(fallback, '点空白处必须能触发图像点击')
  assert.equal(fallback.x1 <= px(100) && px(100) <= fallback.x2, true)
  assert.equal(fallback.y1 <= px(400) && px(400) <= fallback.y2, true)
  assert.ok(fallback.area < 400 * 400, `默认选区不能是整屏：${fallback.area}`)
})

test('tap-area fallback stays inside the screen near the top-left corner', () => {
  const fallback = findBestNodeAtPoint(nodesOf(NOTES), 10, 10)
  assert.equal(fallback.x1, 0)
  assert.equal(fallback.y1, 0)
})

test('real elements stay recordable, smallest labelled element wins', () => {
  const nodes = nodesOf(NOTES)
  assert.equal(findBestRecordableNode(nodes, px(50), px(92)).text, '返回')
  assert.equal(findBestRecordableNode(nodes, px(100), px(255)).className, 'XCUIElementTypeStaticText')
  assert.equal(findBestRecordableNode(nodes, px(100), px(300)).className, 'XCUIElementTypeCell')
})

test('a small unlabelled element becomes the image-click target itself', () => {
  const nodes = nodesOf(page(el('Image', rect(100, 100, 40, 40))))
  const fallback = findBestNodeAtPoint(nodes, px(120), px(120))
  assert.equal(fallback.className, 'XCUIElementTypeImage')
  assert.equal(findBestRecordableNode(nodes, px(120), px(120)), null)
})

test('decorative strips and scroll bars fall back to the area around the tap', () => {
  const nodes = nodesOf(page(
    el('Other', rect(0, 0, 393, 99)), // 真机：顶部装饰层
    el('Other', rect(387, 116, 3, 280)), // 真机：无 label 的滚动条
    el('Other', rect(-16, 674, 425, 610)) // 真机：大半在屏外的底部容器
  ))
  for (const [x, y] of [[200, 50], [388, 200], [200, 800]]) {
    const fallback = findBestNodeAtPoint(nodes, px(x), px(y))
    assert.equal(fallback.className, '', `${x},${y}`)
    assert.equal(fallback.x2 - fallback.x1 <= 160, true)
  }
})

test('the map view is not a recording target (label is live camera JSON)', () => {
  const nodes = nodesOf(page(el('Map', { label: '{camera}', name: 'Map', ...rect(0, 0, 393, 700) }, el('Other', rect(0, 0, 10, 10)))))
  assert.equal(findBestRecordableNode(nodes, px(200), px(300)), null)
})

test('labelled cards stay recordable; screen-sized ones do not', () => {
  const card = nodesOf(page(el('Other', { name: '现已推出', label: '现已推出', ...rect(20, 113, 353, 300) }, el('Image', rect(20, 113, 10, 10)))))
  assert.equal(findBestRecordableNode(card, px(200), px(300)).text, '现已推出')
  // 相册滚动内容是屏幕的 3.5 倍：按屏幕内面积判断才能认出它铺满屏
  const scroll = nodesOf(page(
    el('Other', { name: '视频, 六秒钟', label: '视频, 六秒钟', ...rect(0, -2268, 393, 3037) }, el('Image', rect(0, 0, 10, 10))),
    el('Other', { name: '更多', label: '更多', ...rect(0, 800, 393, 40) })
  ))
  assert.equal(findBestRecordableNode(scroll, px(200), px(300)), null)
})

test('swift runtime names are not treated as text', () => {
  const nodes = nodesOf(page(el('Image', { name: '_TtGC7SwiftUI32NavigationStackHosting', ...rect(0, 0, 100, 100) })))
  assert.equal(findBestRecordableNode(nodes, px(50), px(50)), null)
})

test('android behavior is unchanged', () => {
  const android = [
    { x1: 0, y1: 0, x2: 1080, y2: 2400, className: 'android.widget.FrameLayout', depth: 1, isLeaf: false, childCount: 2, area: 2592000, text: '', contentDesc: '', resourceId: '' },
    { x1: 0, y1: 0, x2: 500, y2: 500, className: 'android.view.View', depth: 5, isLeaf: true, childCount: 0, area: 250000, text: '', contentDesc: '', resourceId: '' }
  ]
  assert.equal(findBestNodeAtPoint(android, 100, 100).className, 'android.view.View')
  assert.equal(findBestNodeAtPoint(android, 900, 2000), null, '安卓没有候选时仍不弹提示')
  assert.equal(findBestRecordableNode(android, 100, 100), null)
})

test('live iOS view has no loading mask; crop mode keeps it', () => {
  const stage = readFileSync(new URL('../src/components/DeviceStage.vue', import.meta.url), 'utf8')
  assert.match(stage, /v-loading="loading && \(!isIosLivePreview \|\| isAnyCropMode\)"/)
  assert.match(stage, /v-show="!isAnyCropMode"/)
})

test('image crop in iOS live view starts from a fresh screenshot', () => {
  const crop = readFileSync(new URL('../src/composables/useScreenCrop.js', import.meta.url), 'utf8')
  const body = crop.match(/const beginQuickImageCapture = async[\s\S]*?\n  const confirmQuickImageCapture/)[0]
  assert.match(body, /if \(isIosLivePreview\.value\) \{[\s\S]*?getDeviceDump[\s\S]*?includeScreenshot: true/)
})
