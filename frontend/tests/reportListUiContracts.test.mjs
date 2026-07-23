import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const reportSource = await readFile(
  new URL('../src/views/reports/ReportList.vue', import.meta.url),
  'utf8',
)
const userStoreSource = await readFile(
  new URL('../src/stores/useUserStore.js', import.meta.url),
  'utf8',
)

test('inspection report rows keep explicit start-time and action columns', () => {
  const paneStart = reportSource.indexOf('label="智能巡检报告"')
  const paneEnd = reportSource.indexOf('</el-tab-pane>', paneStart)
  const inspectionPane = reportSource.slice(paneStart, paneEnd)

  assert.ok(paneStart >= 0 && paneEnd > paneStart, 'inspection report tab must exist')
  assert.match(
    inspectionPane,
    /<el-table-column label="开始时间" width="150" align="center">[\s\S]*?formatDate\(row\.started_at \|\| row\.created_at\)/,
  )
  assert.match(
    inspectionPane,
    /<el-table-column label="操作" width="88" fixed="right" align="center">/,
  )
})

test('report tabs stay synchronized with the route query', () => {
  assert.match(
    reportSource,
    /const syncReportTabQuery = tab => \{[\s\S]*?router\.replace\(\{ query: \{ \.\.\.route\.query, tab: queryTab \} \}\)[\s\S]*?\}/,
  )
  assert.match(reportSource, /<el-tabs v-model="activeTab"[^>]*@tab-change="handleReportTabChange">/)
  assert.match(
    reportSource,
    /const handleReportTabChange = tab => \{[\s\S]*?syncReportTabQuery\(next\)[\s\S]*?handleTabChange\(next\)/,
  )
  assert.match(
    reportSource,
    /watch\(\s*\(\) => route\.query\.tab,[\s\S]*?const next = resolveAvailableTab\(tab\)[\s\S]*?activeTab\.value = next/,
  )
})

test('inspection feature flags fail closed and return a hidden tab to UI reports', () => {
  assert.match(userStoreSource, /const defaultFeatureFlags = \{[\s\S]*?model_inspection:\s*false,/)
  assert.match(
    userStoreSource,
    /catch \{\s*featureFlags\.value = \{ \.\.\.defaultFeatureFlags \}\s*\}/,
  )
  assert.match(
    reportSource,
    /return resolved === 'inspection' && !inspectionEnabled\.value \? 'ui' : resolved/,
  )
  assert.match(
    reportSource,
    /watch\(inspectionEnabled, enabled => \{[\s\S]*?activeTab\.value = 'ui'[\s\S]*?syncReportTabQuery\('ui'\)/,
  )
})

test('mobile inspection metrics wrap and collapse to one column on narrow screens', () => {
  assert.match(
    reportSource,
    /\.mobile-inspection-metrics span, \.mobile-inspection-metrics small\s*\{[^}]*min-width:\s*0;[^}]*overflow-wrap:\s*anywhere;/,
  )
  assert.match(
    reportSource,
    /@media \(max-width:\s*460px\)\s*\{[\s\S]*?\.mobile-inspection-metrics\s*\{\s*grid-template-columns:\s*minmax\(0,\s*1fr\);\s*\}/,
  )
})
