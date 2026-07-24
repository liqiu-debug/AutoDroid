import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const source = await readFile(
  new URL('../src/views/special/Inspection.vue', import.meta.url),
  'utf8',
)

test('inspection run budget reserves the final 15 percent for endpoint reverification', () => {
  assert.match(source, /Math\.floor\(total \* 0\.85\)/)
  assert.doesNotMatch(source, /Math\.floor\(total \* 0\.9\)/)
  assert.match(source, /分钟探索.*分钟验证/)
})

test('inspection run offers 90 and 120 minute deep-coverage budgets', () => {
  assert.match(source, /label: '90 分', value: '90'/)
  assert.match(source, /label: '120 分', value: '120'/)
  assert.match(source, /Math\.min\(120, Math\.max\(5, Number\(runForm\.duration_minutes\)/)
  assert.match(source, /v-model="runForm\.duration_minutes" :min="5" :max="120"/)
})
