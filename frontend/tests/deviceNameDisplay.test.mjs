import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { deviceDisplayName, deviceNameFromRecord } from '../src/utils/deviceName.js'

// 设备展示名口径：custom_name > market_name > model > serial；
// 查不到设备时不把序列号（尤其 iOS UDID）当名字展示。
// 背景（2026-11-08）：稳定性报告 iOS 设备显示设备编号——报告中心用 /fastbot/devices 建
// 设备映射，而该接口默认不返回 iOS 设备。

const UDID = '00008120-00146CA4110BC01E'
const src = path => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')

test('display name priority: custom > market > model > serial', () => {
  assert.equal(deviceDisplayName({ custom_name: '测试机 A', market_name: 'OPPO Reno14 Pro', model: 'PKZ110', serial: 'S1' }), '测试机 A')
  assert.equal(deviceDisplayName({ market_name: 'OPPO Reno14 Pro', model: 'PKZ110', serial: 'S1' }), 'OPPO Reno14 Pro')
  assert.equal(deviceDisplayName({ model: 'iPhone 14 Pro', serial: UDID }), 'iPhone 14 Pro')
  assert.equal(deviceDisplayName({ serial: 'S1' }), 'S1')
  assert.equal(deviceDisplayName(null, '未知设备'), '未知设备')
})

test('record lookup wins over fallback info', () => {
  assert.equal(deviceNameFromRecord(UDID, { model: 'iPhone 14 Pro' }, '桌面客户端 · ' + UDID), 'iPhone 14 Pro')
})

test('a missing device never shows a serial/UDID', () => {
  assert.equal(deviceNameFromRecord(UDID, null, undefined), '未知设备')
  assert.equal(deviceNameFromRecord('3B6F68E6SA7TA0Q5', null, '桌面客户端 · 3B6F68E6SA7TA0Q5'), '未知设备')
  assert.equal(deviceNameFromRecord('emulator-5554', null, undefined, 'Unknown Device'), 'Unknown Device')
})

test('historical device_info formats are understood', () => {
  // 旧格式「型号 (序列号)」
  assert.equal(deviceNameFromRecord('S1', null, 'OPPO Reno14 Pro (3B6F68E6SA7TA0Q5)'), 'OPPO Reno14 Pro')
  // 桌面客户端旧占位「桌面客户端 · 序列号」不是型号；去掉前缀后是名字则照常显示
  assert.equal(deviceNameFromRecord('S1', null, '桌面客户端 · 客厅机'), '客厅机')
  assert.equal(deviceNameFromRecord('S1', null, 'iPhone 14 Pro'), 'iPhone 14 Pro')
})

test('name-like serials and short ids stay visible', () => {
  // 历史行把「刘振宇的iPhone (00008120-…)」存进 device_serial
  assert.equal(deviceNameFromRecord(`刘振宇的iPhone (${UDID})`, null, undefined), '刘振宇的iPhone')
  assert.equal(deviceNameFromRecord('USB1', null, undefined), 'USB1')
  assert.equal(deviceNameFromRecord('', null, undefined, '-'), '-')
})

test('report center maps every platform: it fetches the full device list', () => {
  const reportList = src('views/reports/ReportList.vue')
  const fetchBody = reportList.match(/const fetchDevices = async \(\) => \{[\s\S]*?\n\}/)[0]
  assert.match(fetchBody, /getDeviceList\(\{ timeout: 30000 \}\)/)
  assert.doesNotMatch(fetchBody, /getFastbotDevices/, '/fastbot/devices 默认不含 iOS 设备，不能用来解析设备名')
  // 报告页兜底不再退回设备编号
  assert.doesNotMatch(reportList, /return item\.device_serial/)
})

test('stability report detail survives slow links and missing devices', () => {
  const detail = src('views/fastbot/FastbotReportDetail.vue')
  assert.match(detail, /getDeviceList\(\{ timeout: 30000 \}\)/)
  assert.doesNotMatch(detail, /return identifier\.replace/, '旧行为：查不到设备直接把序列号当名字')
})

test('fluency and startup history rows also resolve iOS device names', () => {
  for (const view of ['views/special/Fluency.vue', 'views/special/Startup.vue']) {
    const source = src(view)
    assert.match(source, /getFastbotDevices\(\{ include_ios: true \}\)/, view)
    assert.doesNotMatch(source, /return serial \|\| '未知设备'/, view)
  }
})

test('device center speaks display names, not raw model codes', () => {
  const center = src('views/devices/DeviceCenter.vue')
  assert.doesNotMatch(center, /\$\{device\.model\}/, '弹窗/提示直接用 model（可能是机型代号 iPhone15,2）')
  assert.match(center, /const deviceTitle = \(device\) => deviceDisplayName\(device/)
  // 自定义名下补一行型号（iOS market_name 已清空，否则型号看不到）
  assert.match(center, /device\.custom_name && !device\.market_name && device\.model/)
})
