import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { computed, ref } from 'vue'

// 客户端有线 iPhone 录制：设备本机占用（跑稳定性测试）时各选择入口的展示与录制拦截。
// 与仓库既有测试一致，用 node:test + 依赖注入执行源码（非 vitest）。

const stripImports = source => source.replace(/^import [\s\S]*? from ['"][^'"]+['"]\s*\n/gm, '')
const srcFile = path => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')

function deviceStreams(devices, selected) {
  const source = stripImports(srcFile('composables/useDeviceStreams.js'))
    .replace('export function useDeviceStreams', 'function useDeviceStreams')
  const deps = {
    computed,
    ref,
    api: {},
    getErrorDetail: () => '',
    parseResolution: () => ({ width: 0, height: 0 }),
    createGatedTask: fn => fn,
    createPollingLoop: () => ({ start() {}, stop() {} }),
    pollRequestConfig: () => ({})
  }
  const useDeviceStreams = new Function(...Object.keys(deps), source + '\nreturn useDeviceStreams')(...Object.values(deps))
  const streams = useDeviceStreams({ liveMode: ref(true), isStageActive: ref(true) })
  streams.connectedDevices.value = devices
  streams.selectedSerial.value = selected
  return streams
}

const IPHONE = { serial: '00008120-00146CA4110BC01E', platform: 'ios' }

test('a device running a stability test counts as busy for recording', () => {
  for (const [status, busy] of [['IDLE', false], ['BUSY', true], ['FASTBOT_RUNNING', true], ['OFFLINE', false]]) {
    const streams = deviceStreams([{ ...IPHONE, status }], IPHONE.serial)
    assert.equal(streams.isSelectedDeviceBusy.value, busy, status)
  }
})

test('a busy iPhone stays observable through the live view', () => {
  const streams = deviceStreams([{ ...IPHONE, status: 'FASTBOT_RUNNING' }], IPHONE.serial)
  assert.equal(streams.isIosLivePreview.value, true)
  assert.equal(streams.canObserveDeviceInCurrentMode({ status: 'FASTBOT_RUNNING' }), true)
})

test('every UI automation device picker explains a stability run', () => {
  for (const view of ['views/cases/CaseList.vue', 'views/cases/CaseEditor.vue', 'views/scenarios/ScenarioList.vue', 'views/scenarios/ScenarioEditor.vue']) {
    const match = srcFile(view).match(/const deviceUnavailableReason = \(device\) => \{[\s\S]*?\n\s*\}\n/)
    assert.ok(match, view)
    const reason = new Function(`${match[0]}\nreturn deviceUnavailableReason`)()
    assert.equal(reason({ status: 'FASTBOT_RUNNING' }), '正在跑稳定性测试', view)
    assert.equal(reason({ status: 'IDLE' }), '', view)
  }
})

test('the recording device picker labels a stability run instead of showing the raw status', () => {
  const stage = srcFile('components/DeviceStage.vue')
  assert.match(stage, /const statusLabel = [\s\S]*?FASTBOT_RUNNING: '🔴 跑测中'/)
  assert.match(stage, /const statusTagType = [\s\S]*?FASTBOT_RUNNING: 'danger'/)
})

test('live view shows the concrete reason when WDA is unavailable', () => {
  const player = srcFile('components/IosMjpegPlayer.vue')
  const body = player.match(/if \(code === 4005[\s\S]*?\n  \}\n/)
  assert.ok(body)
  const run = reason => {
    const status = { value: '' }
    const statusDetail = { value: '' }
    new Function('code', 'reason', 'status', 'statusDetail', 'emit', `${body[0]}`)(4005, reason, status, statusDetail, () => {})
    return statusDetail.value
  }
  assert.equal(run('P1005_WDA_UNAVAILABLE: iOS 设备未通过 USB 连接或未信任本机'), 'WDA 不可用：iOS 设备未通过 USB 连接或未信任本机')
  assert.equal(run('P1005_WDA_UNAVAILABLE'), 'WDA 不可用，请到「设备中心」检测/启动 WDA 后重试')
})
