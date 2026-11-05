import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// 智能稳定性「异常回放」UI 契约：
// - 表单开关默认开启（关掉才不发 enable_local_replay=false）；
// - 报告侧只认 summary.local_replay_enabled，客户端本地执行降级（录制起不来）时
//   后端会在 summary.degraded 里带 local_replay，报告要说明原因——否则
//   「表单开着 / 报告未开启」看起来像配置丢了（2026-11-04 真机反馈）；
// - 崩溃行只在 replay.status === READY 且文件名存在时给「查看回放」入口。
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')

test('fastbot dashboard keeps local replay on by default and in the payload', () => {
  const dashboard = read('../src/views/fastbot/FastbotDashboard.vue')
  assert.match(dashboard, /enable_local_replay:\s*true/, '异常回放默认开启')
  assert.match(
    dashboard,
    /enable_local_replay:\s*form\.enable_local_replay/,
    '开关值必须进 run 载荷',
  )
})

test('report explains a degraded local replay instead of a bare 未开启', () => {
  const detail = read('../src/views/fastbot/FastbotReportDetail.vue')
  assert.match(detail, /summary\.value\?\.local_replay_enabled === true/)
  assert.match(detail, /degraded\.includes\('local_replay'\)/)
  assert.match(detail, /未开启（客户端降级）/)
  assert.match(detail, /客户端未能启动本地回放录制/)
})

test('crash rows expose the replay entry only for a READY replay file', () => {
  const formatters = read('../src/views/fastbot/components/reportFormatters.js')
  assert.match(formatters, /replay\?\.status === 'READY' && Boolean\(getReplayFilename\(event\)\)/)
  const card = read('../src/views/fastbot/components/CrashEventsCard.vue')
  assert.match(card, /view-replay/)
  assert.match(card, /localReplayEnabled/)
})
