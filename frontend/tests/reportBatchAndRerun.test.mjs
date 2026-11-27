import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const apiSource = await readFile(new URL('../src/api/index.js', import.meta.url), 'utf8')
const reportListSource = await readFile(
  new URL('../src/views/reports/ReportList.vue', import.meta.url),
  'utf8',
)
const reportDetailSource = await readFile(
  new URL('../src/views/reports/ReportDetail.vue', import.meta.url),
  'utf8',
)

test('report center exposes batch delete endpoint, selection column, and failed-device rerun', () => {
  assert.match(apiSource, /batchDeleteReports\(payload\)\s*\{\s*return api\.post\('\/reports\/batch-delete', payload\)/)
  assert.match(reportListSource, /type="selection"[^/]*:selectable="row => !isExecutionRunning\(row\.status\)"/)
  assert.match(reportListSource, /批量删除 \(\{\{ selectedBatchRows\.length \}\}\)/)
  assert.match(reportListSource, /api\.batchDeleteReports\(payload\)/)
  assert.match(reportListSource, /重跑失败设备 \(\{\{ selectedBatchFailedExecutions\.length \}\}\)/)
  assert.match(reportListSource, /submitExecution\(api,\s*\{\s*kind:\s*'scenario'/)
})

test('report detail exposes one-click rerun for scenario executions and reacts to route id changes', () => {
  assert.match(reportDetailSource, /canRerunExecution\s*=\s*computed\(\(\)\s*=>\s*Number\(execution\.value\?\.scenario_id\)\s*>\s*0\s*&&\s*Boolean\(execution\.value\?\.device_serial\)/)
  assert.match(reportDetailSource, /@click="handleRerunExecution"[\s\S]*?重新执行/)
  assert.match(reportDetailSource, /watch\(\s*\(\) => route\.params\.id/)
})
