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
    /watch\(\s*\(\) => route\.query\.tab,[\s\S]*?const next = resolveTab\(tab\)[\s\S]*?activeTab\.value = next/,
  )
})

test('feature flags fail closed to the defaults', () => {
  assert.match(userStoreSource, /const defaultFeatureFlags = \{[\s\S]*?content_addressed_assets:\s*false,/)
  assert.match(
    userStoreSource,
    /catch \{\s*featureFlags\.value = \{ \.\.\.defaultFeatureFlags \}\s*\}/,
  )
})
