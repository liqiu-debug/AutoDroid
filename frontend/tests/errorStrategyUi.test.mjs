import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { ref, computed, nextTick } from 'vue'
import * as actionConstants from '../src/utils/actionConstants.js'

// 容错策略语义（docs/EXECUTION_SPEC.md 2.2）前端侧：
// - IGNORE 的失败记 SKIP（skip_reason="失败已忽略：<原始 error>"），不再把 PASS 用例降级成 WARNING；
// - 结论只由未被容忍的失败决定，全 SKIP 用例才归 skipped；
// - 结论条 通过+失败+告警+跳过 = 总数（跳过按残差计，与后端 HTML 报告同口径）；
// - 步骤行"跳过原因"优先读 report_display.skip_reason，缺失时回退 error_message。
// 步骤卡展开态按 step.uuid 记录，删除/排序/拖入都不会展开错卡。

const stripImports = source => source.replace(/^import [\s\S]*? from ['"][^'"]+['"]\s*\n/gm, '')
const scriptOf = name => stripImports(
  readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8').match(/<script setup>([\s\S]*?)<\/script>/)[1],
)
const sourceOf = name => readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8')
const quietMessages = () => Object.fromEntries(
  ['success', 'warning', 'error', 'info'].map(kind => [kind, () => {}]),
)

function reportDetailSetup(steps) {
  const source = scriptOf('views/reports/ReportDetail.vue')
  const dependencies = {
    ref, computed, onMounted: () => {},
    useRoute: () => ({ params: { id: '7' }, query: {} }),
    useRouter: () => ({ back: () => {} }),
    ElMessage: quietMessages(),
    api: {
      getReport: async () => ({ data: { id: 7, scenario_name: '下单场景', status: 'PASS', steps } }),
      getDeviceList: async () => ({ data: [] }),
      getReports: async () => ({ data: { items: [] } }),
      getReportAssetUrl: path => `/api/static/${path}`,
    },
    deviceNameFromRecord: () => 'Pixel 7',
    dayjs: () => ({ format: () => '2026-11-11 00:00:00' }),
    ACTION_LABELS: actionConstants.ACTION_LABELS,
    useClientMode: () => ({ isMobileMode: ref(false) }),
    getStatusTagType: () => 'info',
    runSourceLabel: () => '',
  }
  const exports = 'cases,resultSummary,fetchDetail,formatStepMessage,getStepSkipReason,isIgnoredFailureStep,getStepErrorCode,getStepSuggestion,hasFailureScreenshot,normalizeStatus'
  const state = new Function(...Object.keys(dependencies), source + `\nreturn {${exports}}`)(...Object.values(dependencies))
  return state
}

const step = (over = {}) => ({
  id: 1,
  step_name: '[登录用例] 点击 登录',
  step_order: 1,
  status: 'PASS',
  duration: 10,
  error_message: null,
  screenshot_path: null,
  report_display: { display_text: '点击 登录' },
  ...over,
})

const ignoredFailure = (over = {}) => step({
  step_name: '[登录用例] 文本断言 包含 下单成功',
  status: 'SKIP',
  error_message: '文本断言失败: 期望包含"下单成功"',
  screenshot_path: 'screenshots/exec_7_step_2.png',
  report_display: {
    display_text: '文本断言 包含 下单成功',
    skip_reason: '失败已忽略：文本断言失败: 期望包含"下单成功"',
    error_code: 'E2003_ASSERT_TEXT_FAILED',
    suggestion: '核对期望文本与页面实际文案是否一致',
  },
  ...over,
})

async function summarize(steps) {
  const state = reportDetailSetup(steps)
  await state.fetchDetail()
  return state
}

test('被 IGNORE 容忍的失败记 SKIP，用例结论仍是 PASS 且结论条四桶闭合', async () => {
  const state = await summarize([step(), ignoredFailure()])
  assert.equal(state.cases.value.length, 1)
  assert.equal(state.cases.value[0].status, 'PASS', 'SKIP 不再把 PASS 用例降级成 WARNING')
  assert.deepEqual({ ...state.resultSummary.value }, { total: 2, passed: 1, failed: 0, warnings: 0, skipped: 1 })
  const counts = state.resultSummary.value
  assert.equal(counts.passed + counts.failed + counts.warnings + counts.skipped, counts.total, '通过+失败+告警+跳过 = 总数')
})

test('用例结论只由未被容忍的失败决定：失败判 FAIL、全跳过归 skipped', async () => {
  const failed = await summarize([step(), step({ status: 'FAIL', error_message: '元素未找到' })])
  assert.equal(failed.cases.value[0].status, 'FAIL')

  const allSkipped = await summarize([
    ignoredFailure(),
    step({ step_name: '[登录用例] 启动应用', status: 'SKIP', report_display: { display_text: '启动应用', skip_reason: '平台不匹配' } }),
  ])
  assert.equal(allSkipped.cases.value[0].status, 'skipped', '全 SKIP（含全被忽略）表示没真正验证到内容')

  const mixed = await summarize([
    step(),
    step({ status: 'WARNING', error_message: '告警' }),
    step({ status: 'FAIL', error_message: '失败' }),
    step({ status: 'SKIP', report_display: { display_text: '等待元素', skip_reason: '上游步骤失败，中止执行' } }),
  ])
  assert.equal(mixed.cases.value[0].status, 'FAIL')
  assert.deepEqual({ ...mixed.resultSummary.value }, { total: 4, passed: 1, failed: 1, warnings: 1, skipped: 1 })
})

test('步骤行跳过原因优先读 report_display.skip_reason，缺失时回退 error_message', async () => {
  const state = await summarize([
    ignoredFailure(),
    step({ step_name: '[登录用例] 等待元素', status: 'SKIP', report_display: { display_text: '等待元素', skip_reason: '上游步骤失败，中止执行' } }),
    step({ step_name: '[登录用例] 启动应用', status: 'SKIP', error_message: 'P1001: 当前平台不允许执行该步骤' }),
    step({ step_name: '[登录用例] 点击 提交', status: 'PASS', error_message: '残留错误文本' }),
  ])
  const [ignored, aborted, platform, passed] = state.cases.value[0].steps
  assert.equal(state.formatStepMessage(ignored), '跳过原因: 失败已忽略：文本断言失败: 期望包含"下单成功"')
  assert.equal(state.formatStepMessage(aborted), '跳过原因: 上游步骤失败，中止执行')
  assert.equal(state.formatStepMessage(platform), '跳过原因: P1001: 当前平台不允许执行该步骤')
  assert.equal(state.formatStepMessage(passed), '残留错误文本', '非跳过步骤沿用 error 原文，不加跳过前缀')
})

test('被容忍失败的错误码、建议与截图仍作为详情留痕', async () => {
  const state = await summarize([ignoredFailure()])
  const row = state.cases.value[0].steps[0]
  assert.equal(state.isIgnoredFailureStep(row), true)
  assert.equal(state.getStepErrorCode(row), 'E2003_ASSERT_TEXT_FAILED')
  assert.equal(state.getStepSuggestion(row), '核对期望文本与页面实际文案是否一致')
  assert.equal(state.hasFailureScreenshot(row), true)

  const platformSkip = step({ status: 'SKIP', report_display: { display_text: '启动应用', skip_reason: '平台不匹配' } })
  assert.equal(state.isIgnoredFailureStep(platformSkip), false)
  assert.equal(state.getStepErrorCode(platformSkip), '')
  assert.equal(state.hasFailureScreenshot(platformSkip), false)
})

function stepBuilderSetup(steps) {
  const source = scriptOf('components/StepBuilder.vue')
  const currentCase = ref({ id: 9, name: '用例', variables: [], steps })
  const lastAddedStepUuid = ref(null)
  const dependencies = {
    ref, computed, nextTick,
    onMounted: () => {}, watch: () => {},
    defineProps: () => ({ envId: null, deviceSerial: '', activeImageCropStepUuid: '', includeScreenshot: true }),
    defineEmits: () => () => {},
    useCaseStore: () => ({}),
    storeToRefs: () => ({ currentCase, lastAddedStepUuid }),
    VueDraggable: {},
    apiClient: {}, api: {},
    ElMessage: quietMessages(),
    createUuid: () => `generated-${Math.random().toString(16).slice(2)}`,
    ...actionConstants,
  }
  const exports = 'currentCase,lastAddedStepUuid,expandedSteps,toggleExpand,isExpanded,ensureStepUuid,errorStrategyOptions,errorStrategyHint'
  const state = new Function(...Object.keys(dependencies), source + `\nreturn {${exports}}`)(...Object.values(dependencies))
  return state
}

const uiStep = (uuid, action = 'click') => ({ uuid, action, selector: '登录', selector_type: 'text', value: '', options: {}, description: '', timeout: 10, error_strategy: 'ABORT', retry_count: 0, execute_on: ['android', 'ios'], platform_overrides: { android: null } })

test('展开态按 step.uuid 记录：删除、排序、拖入都不会展开错卡', () => {
  const state = stepBuilderSetup([uiStep('u1'), uiStep('u2'), uiStep('u3')])
  const steps = state.currentCase.value.steps
  state.toggleExpand(steps[0])
  assert.equal(state.isExpanded(steps[0]), true)
  assert.equal(state.isExpanded(steps[1]), false)

  // 删除被展开的卡：不应把展开态落到顶上来的下一张卡
  steps.splice(0, 1)
  assert.equal(state.isExpanded(steps[0]), false, '删除后 index 0 不继承展开态')
  assert.equal(state.isExpanded(steps[1]), false)

  // 拖拽排序：展开态跟着 uuid 走，不跟着下标走
  state.toggleExpand(steps[1]) // 展开 u3
  const [moved] = steps.splice(1, 1)
  steps.unshift(moved)
  assert.deepEqual(steps.map(s => s.uuid), ['u3', 'u2'])
  assert.equal(state.isExpanded(steps[0]), true, 'u3 移到首位仍是展开态')
  assert.equal(state.isExpanded(steps[1]), false, 'u2 不因占据旧下标而展开')

  // 从 GeneralStepsPanel 拖入（新卡带 uuid、可插在中间）：互不影响
  const dragged = uiStep('u9', 'sleep')
  steps.splice(1, 0, dragged)
  assert.deepEqual(steps.map(s => s.uuid), ['u3', 'u9', 'u2'])
  assert.equal(state.isExpanded(dragged), false, '新拖入的卡不继承下标上的展开态')
  state.toggleExpand(dragged)
  assert.equal(state.isExpanded(dragged), true)
  assert.equal(state.isExpanded(steps[0]), true, 'u3 仍是展开态')
  assert.equal(state.isExpanded(steps[2]), false)
})

test('无 uuid 的步骤补 uuid，展开态仍有稳定键', () => {
  const state = stepBuilderSetup([{ action: 'home' }])
  const [legacy] = state.currentCase.value.steps
  assert.ok(state.ensureStepUuid(legacy), '缺 uuid 的步骤补上稳定键')
  assert.equal(Boolean(legacy.uuid), true)
  state.toggleExpand(legacy)
  assert.equal(state.isExpanded(legacy), true)
})

test('容错策略选项说明新语义：中止=中止用例、继续=用例判失败、忽略=记跳过不影响结论', () => {
  const source = sourceOf('components/StepBuilder.vue')
  assert.match(source, /v-for="opt in errorStrategyOptions"/)
  const state = stepBuilderSetup([uiStep('u1')])
  const byValue = Object.fromEntries(state.errorStrategyOptions.map(opt => [opt.value, opt.description]))
  assert.deepEqual(Object.keys(byValue).sort(), ['ABORT', 'CONTINUE', 'IGNORE'])
  assert.equal(byValue.ABORT, '失败即中止用例，后续步骤不再执行')
  assert.equal(byValue.CONTINUE, '失败但继续执行，用例判失败')
  assert.equal(byValue.IGNORE, '失败记为跳过，不影响用例结论')
  assert.equal(state.errorStrategyHint('ABORT'), '失败即中止用例，后续步骤不再执行')
  assert.equal(state.errorStrategyHint('CONTINUE'), '失败但继续执行，用例判失败')
  assert.equal(state.errorStrategyHint('IGNORE'), '失败记为跳过，不影响用例结论')
})
