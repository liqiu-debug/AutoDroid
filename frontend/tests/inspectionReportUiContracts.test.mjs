import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const source = await readFile(
  new URL('../src/views/reports/InspectionReportDetail.vue', import.meta.url),
  'utf8',
)

test('inspection report keeps three primary metrics and moves diagnostics behind disclosure', () => {
  const template = source.slice(source.indexOf('<template>'))
  const stats = template.slice(template.indexOf('class="stats"'), template.indexOf('class="running-summary"'))
  assert.match(stats, /核心旅程/)
  assert.match(stats, /运行范围/)
  assert.match(stats, /businessCoverage\.scopeSelected/)
  assert.match(stats, /证据质量/)
  assert.doesNotMatch(stats, /页面族覆盖/)
  assert.equal((stats.match(/<strong/g) || []).length, 3)
  assert.match(template, /<el-collapse-item name="diagnostics" title="运行诊断">/)
  assert.match(source, /已发现页面族展开率/)
  assert.match(source, /family\.ratio \* 1000\) \/ 10/)
  assert.match(template, /label="核心旅程" name="coverage"/)
  assert.match(template, /coverageBlindSpots/)
  assert.match(template, /evidenceTransitions\(row\)/)
  assert.match(template, /<em>页面<\/em>/)
  assert.match(template, /<em>动作<\/em>/)
})

test('historical coverage without stage data does not render a misleading zero progress', () => {
  assert.match(source, /row\.total_stages > 0/)
  assert.match(source, /: '-'/)
  assert.doesNotMatch(source, /row\.deepest_stage \|\| 0 }}\/{{ row\.total_stages \|\| 0/)
})

test('installed replay entry follows evidence availability with a legacy fallback', () => {
  const replayGuard = source.slice(
    source.indexOf('const replaySourceEligible'),
    source.indexOf('const replaySourceReason'),
  )
  assert.match(replayGuard, /replay_evidence_available/)
  assert.match(replayGuard, /replayEvidenceAvailable\.value/)
  assert.match(replayGuard, /replaySourceEligible\.value/)
})

test('state snapshot shows user outcomes and keeps internal fields in technical details', () => {
  const template = source.slice(source.indexOf('<template>'))
  const drawer = template.slice(template.indexOf('<el-drawer'), template.indexOf('class="technical-details"'))
  assert.match(drawer, /label="页面"/)
  assert.match(drawer, /label="巡检结果"/)
  assert.match(drawer, /label="升级回放"/)
  assert.match(drawer, /v-if="selectedNodeHasBoundary" label="停止原因"/)
  assert.doesNotMatch(drawer, /内部 State ID|实例锚点|语义键/)
  assert.match(template, /label="内部 State ID"/)
})

test('page tree exposes proportional navigation controls without dimming other branches', () => {
  assert.match(source, /symbolKeepAspect:\s*false/)
  assert.match(source, /aria-label="放大"/)
  assert.match(source, /aria-label="缩小"/)
  assert.match(source, /aria-label="定位当前或已选页面"/)
  assert.match(source, /emphasis:\s*\{ focus:\s*'none'/)
})

test('page tree captures the pointer only after crossing the drag threshold', () => {
  const pointerDown = source.slice(
    source.indexOf('const handleMindMapPointerDown'),
    source.indexOf('const handleMindMapPointerMove'),
  )
  const pointerMove = source.slice(
    source.indexOf('const handleMindMapPointerMove'),
    source.indexOf('const handleMindMapPointerUp'),
  )
  const thresholdIndex = pointerMove.indexOf('Math.hypot(dx, dy) < 4')
  const captureIndex = pointerMove.indexOf('setPointerCapture')

  assert.doesNotMatch(pointerDown, /setPointerCapture/)
  assert.ok(thresholdIndex >= 0, 'drag threshold must be checked')
  assert.ok(captureIndex > thresholdIndex, 'pointer capture must happen after the threshold check')
  assert.match(pointerMove, /if \(!isMindMapDragging\.value\) viewport\.setPointerCapture\?\.\(event\.pointerId\)/)
})

test('diagnostic values can wrap without overflowing their grid cells', () => {
  assert.match(
    source,
    /\.diagnostics-grid span\s*\{[^}]*min-width:\s*0;[^}]*grid-template-columns:\s*max-content minmax\(0,\s*1fr\);/,
  )
  assert.match(
    source,
    /\.diagnostics-grid strong\s*\{[^}]*min-width:\s*0;[^}]*overflow-wrap:\s*anywhere;[^}]*white-space:\s*normal;/,
  )
})

test('wrapped coverage blind spots cannot shrink into adjacent report sections', () => {
  const style = source.match(/\.coverage-blind-spots\s*\{([^}]*)\}/)?.[1] || ''
  assert.match(style, /flex:\s*none;/)
  assert.match(style, /flex-wrap:\s*wrap;/)
})
