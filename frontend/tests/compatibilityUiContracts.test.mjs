import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const creationSource = await readFile(
  new URL('../src/views/special/Compatibility.vue', import.meta.url),
  'utf8',
)
const reportSource = await readFile(
  new URL('../src/views/reports/CompatibilityReportDetail.vue', import.meta.url),
  'utf8',
)

// 兼容性测试只保留「手动配置页面合集」一条路：创建表单常驻可见，
// 来源固定为 page_set，对比维度只剩版本 / 机型。
test('compatibility creation keeps the manual page-set flow only', () => {
  assert.match(creationSource, /新建兼容性测试/)
  assert.match(creationSource, /source_type: 'page_set'/)
  assert.match(creationSource, /label: '版本对比', value: 'version'/)
  assert.match(creationSource, /label: '机型对比', value: 'device'/)
  assert.match(creationSource, /页面合集/)
  // 巡检来源 / 升级后链路回放已下线
  assert.doesNotMatch(creationSource, /inspection_run_id|replayForm|installed_replay|巡检来源|升级后链路回放/)
  // 创建入口不再受灰度开关隐藏
  assert.doesNotMatch(creationSource, /legacyCreationEnabled|compatibility_legacy_compare_creation/)
})

test('compatibility report renders page-set results without inspection or replay fields', () => {
  assert.match(reportSource, /page_set/)
  assert.doesNotMatch(reportSource, /source_state_id|source_observation_id|State ID|Observation ID/)
  assert.doesNotMatch(reportSource, /replay_boundary_evidence|source_boundary_evidence|chain_id|path_key/)
})
