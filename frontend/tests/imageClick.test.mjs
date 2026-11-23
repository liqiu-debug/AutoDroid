import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { ref, computed, nextTick, watch, reactive, effectScope, isProxy } from 'vue'
import * as imageOptions from '../src/utils/imageOptions.js'
import * as actionConstants from '../src/utils/actionConstants.js'

const read = file => readFileSync(new URL(file, import.meta.url), 'utf8')
const stripImports = source => source.replace(/^import [\s\S]*? from ['"][^'"]+['"]\s*\n/gm, '')
const copy = value => JSON.parse(JSON.stringify(value))
const sha = 'a'.repeat(64)
const config = { template_sha256: sha, tap_anchor: [0.2, 0.7], source_size: [1080, 2400], search_region: [0, 0.3, 1, 1] }
const maps = { android: config, ios: { ...config, template_sha256: 'b'.repeat(64), tap_anchor: [0.8, 0.1] } }
const notices = { info() {}, warning() {}, success() {}, error() {} }

function storeFunctions() {
  const source = stripImports(read('../src/stores/useCaseStore.js')).replace('export const useCaseStore', 'const useCaseStore')
  const deps = { defineStore: (_id, factory) => factory, createUuid: () => 'test-step', ...imageOptions }
  return new Function(...Object.keys(deps), source + '\nreturn {standardStepToUiStep,uiStepToStandardStep,uiStepToLegacyStep}')(...Object.values(deps))
}

test('saved image settings survive load, edit, save and legacy export for both platforms', () => {
  const { standardStepToUiStep, uiStepToStandardStep, uiStepToLegacyStep } = storeFunctions()
  const saved = { action: 'click_image', args: { image_path: 'static/images/a.png', image_options: copy(maps), future_option: { enabled: true } }, platform_overrides: { android: { selector: 'static/images/a.png', by: 'image' }, ios: { selector: 'static/images/b.png', by: 'image' } } }
  const ui = standardStepToUiStep(saved)
  assert.deepEqual(ui.options.image_options, maps)
  const edited = imageOptions.getImageOptionsMap(ui)
  edited.android.tap_anchor = [0.3, 0.6]
  imageOptions.setImageOptionsMap(ui, edited)
  const standard = uiStepToStandardStep(ui, 1)
  assert.deepEqual(standard.args.image_options, edited)
  assert.deepEqual(standard.args.future_option, { enabled: true })
  assert.deepEqual(uiStepToLegacyStep(ui).options.image_options, edited)
  assert.deepEqual(standardStepToUiStep(standard).options.image_options, edited)
  assert.deepEqual(saved.args.image_options, maps, 'editing must not mutate the fetched source object')
})

test('shared descriptors survive assertion and click legacy round trips without null defaults', () => {
  const { standardStepToUiStep, uiStepToStandardStep, uiStepToLegacyStep } = storeFunctions()
  const shared = { profile: 'adaptive-v2', template_sha256: sha, descriptor_canonical: '{"text":"云闪付"}', descriptor_sha256: 'c'.repeat(64) }
  for (const action of ['click_image', 'assert_image']) {
    const saved = { action, args: { image_path: 'shared.png', image_options: { ios: config }, image_match: shared } }
    const ui = standardStepToUiStep(saved)
    assert.deepEqual(uiStepToStandardStep(ui, 1).args.image_match, shared)
    assert.deepEqual(uiStepToLegacyStep(ui).options.image_match, shared)
    assert.deepEqual(uiStepToLegacyStep(ui).options.image_options, { ios: config })
    for (const invalid of [null, false, '', [], { profile: null }, { profile: 'unknown' }]) {
      assert.throws(() => standardStepToUiStep({ action, args: { image_match: invalid } }), /配置/)
      assert.throws(() => uiStepToStandardStep({ action, args: { image_match: invalid }, options: {} }, 1), /配置/)
    }
  }
  assert.throws(() => imageOptions.getSharedImageMatch({ args: { image_match: shared }, options: { image_match: { profile: 'strict-v1' } } }), /冲突/)
  assert.deepEqual(imageOptions.getSharedImageMatch({ args: { image_match: { profile: 'strict-v1' } } }), { profile: 'strict-v1' })
  const newStep = { action: 'assert_image', selector: '', options: {} }
  const before = copy(newStep)
  assert.throws(() => imageOptions.bindRecordedImage(newStep, 'ios', 'new.png', config, null), /配置/)
  assert.deepEqual(newStep, before, 'invalid metadata must not partially replace the template')
})

test('recrop retains explicit strict semantics and independent template hashes', () => {
  const step = { action: 'click_image', selector: 'old.png', args: { image_path: 'old.png', image_match: { profile: 'strict-v1' } },
    options: { image_match: { profile: 'strict-v1' }, image_options: { android: config } } }
  imageOptions.bindRecordedImage(step, 'ios', 'new.png', maps.ios, { profile: 'adaptive-v2', template_sha256: 'b'.repeat(64) })
  assert.deepEqual(step.args.image_match, { profile: 'strict-v1' })
  assert.equal(step.platform_overrides.android.selector, 'old.png')
  assert.equal(step.options.image_options.android.template_sha256, sha)
  assert.equal(step.options.image_options.ios.template_sha256, 'b'.repeat(64))
  imageOptions.replaceDefaultImagePath(step, 'third.png')
  assert.deepEqual(step.args.image_match, { profile: 'strict-v1' })
})

test('independent legacy pictures freeze only after selecting their original template', () => {
  const shared = { profile: 'adaptive-v2', template_sha256: sha, descriptor_canonical: '{}', descriptor_sha256: 'c'.repeat(64) }
  const step = { action: 'click_image', selector: 'old.png', args: { image_path: 'old.png', image_match: shared },
    options: { image_match: copy(shared), image_options: { android: config } } }
  imageOptions.bindRecordedImage(step, 'ios', 'new.png', maps.ios, { ...shared, template_sha256: 'b'.repeat(64) })
  assert.deepEqual(step.args.image_match, { profile: 'adaptive-v2' })
  assert.equal(imageOptions.selectImagePath(step, 'android'), 'old.png')
  assert.equal(imageOptions.selectImagePath(step, 'ios'), 'new.png')
})

test('an explicitly cleared template binding cannot resurrect old args metadata', () => {
  const { uiStepToStandardStep } = storeFunctions()
  const step = { action: 'click_image', selector: 'static/images/new.png', selector_type: 'image', args: { image_options: copy(maps) }, options: {} }
  imageOptions.clearImageOptions(step)
  assert.deepEqual(uiStepToStandardStep(step, 1).args.image_options, {})
  assert.equal(imageOptions.selectImageOptions(step, 'ios'), null)
  const onlyAndroid = { options: { image_options: { android: config } } }
  assert.deepEqual(imageOptions.selectImageOptions(onlyAndroid, 'ios'), { template_sha256: sha, tap_anchor: config.tap_anchor })
})

test('single-platform image fallback preserves its fingerprint and anchor without screen geometry', () => {
  for (const [platform, other] of [['android', 'ios'], ['ios', 'android']]) {
    const step = { action: 'click_image', args: { image_path: 'recorded.png', image_options: { [other]: copy(config) } },
      platform_overrides: { [other]: { selector: 'recorded.png', by: 'image' } } }
    assert.equal(imageOptions.selectImagePath(step, platform), 'recorded.png')
    assert.deepEqual(imageOptions.selectImageOptions(step, platform), { template_sha256: sha, tap_anchor: config.tap_anchor })
    assert.deepEqual(imageOptions.selectImageOptions(step, other), config)
    assert.deepEqual(step.args.image_options[other], config)
  }
})

test('current platform and default bindings retain their complete configuration', () => {
  const defaults = { ...config, template_sha256: 'd'.repeat(64) }
  const step = { args: { image_path: 'shared.png', image_options: { ...copy(maps), default: defaults } } }
  assert.deepEqual(imageOptions.selectImageOptions(step, 'android'), config)
  assert.deepEqual(imageOptions.selectImageOptions(step, 'ios'), maps.ios)
  delete step.args.image_options.ios
  assert.deepEqual(imageOptions.selectImageOptions(step, 'ios'), defaults)
})

test('image timeout messages distinguish absence, capture failure and movement', () => {
  assert.match(imageOptions.imageMatchStatusText({ status: 'not_found', reason: 'no_verified_candidate', timed_out: true }), /在超时内未找到通过验证的目标/)
  assert.match(imageOptions.imageMatchStatusText({ status: 'unstable', reason: 'screenshot_deadline' }), /获取设备截图超时/)
  assert.match(imageOptions.imageMatchStatusText({ status: 'unstable', reason: 'target_not_stable' }), /目标未在超时内稳定/)
  assert.match(imageOptions.imageMatchStatusText({ status: 'not_found', reason: 'no_verified_candidate' }), /当前画面未找到目标/)
})

test('recropping iOS preserves an unbound legacy Android template without borrowing the iOS fingerprint', () => {
  const step = { action: 'click_image', selector: 'android-old.png', selector_type: 'image',
    args: { image_path: 'android-old.png' }, platform_overrides: { android: { by: 'image', selector: 'android-old.png' } } }
  imageOptions.bindRecordedImage(step, 'ios', 'ios-new.png', copy(config))
  assert.equal(imageOptions.selectImagePath(step, 'android'), 'android-old.png')
  assert.equal(imageOptions.selectImageOptions(step, 'android'), null)
  assert.deepEqual(imageOptions.selectImageOptions(step, 'ios'), config)
})

test('recropping the only recorded platform preserves the shared template binding on the other platform', () => {
  const step = { action: 'click_image', selector: 'ios-old.png', args: { image_path: 'ios-old.png', image_options: { ios: copy(config) } },
    platform_overrides: { ios: { by: 'image', selector: 'ios-old.png' } } }
  const replacement = { ...config, template_sha256: 'f'.repeat(64) }
  imageOptions.bindRecordedImage(step, 'ios', 'ios-new.png', replacement)
  assert.equal(imageOptions.selectImagePath(step, 'android'), 'ios-old.png')
  assert.deepEqual(imageOptions.selectImageOptions(step, 'android'), { template_sha256: sha, tap_anchor: config.tap_anchor })
  assert.deepEqual(imageOptions.selectImageOptions(step, 'ios'), replacement)
})

test('recropping only iOS preserves the Android template and metadata through store conversion', () => {
  const { standardStepToUiStep, uiStepToStandardStep } = storeFunctions()
  const step = standardStepToUiStep({ action: 'click_image', args: { image_path: 'android.png', image_options: { default: config } }, platform_overrides: { android: { selector: 'android.png', by: 'image' } } })
  const ios = { ...config, template_sha256: 'c'.repeat(64), tap_anchor: [0.9, 0.1] }
  imageOptions.bindRecordedImage(step, 'ios', 'ios-new.png', ios)
  const saved = uiStepToStandardStep(step, 1)
  assert.equal(saved.args.image_path, 'android.png')
  assert.equal(saved.platform_overrides.android.selector, 'android.png')
  assert.equal(saved.platform_overrides.ios.selector, 'ios-new.png')
  assert.deepEqual(saved.args.image_options.android, config)
  assert.deepEqual(saved.args.image_options.ios, ios)
  assert.equal(saved.args.image_options.default, undefined)
})

test('changing the default path drops its old binding and preserves an independent iOS template', () => {
  const step = { action: 'click_image', selector: 'android-old.png', args: { image_path: 'android-old.png' }, options: { image_options: copy(maps) }, platform_overrides: { ios: { selector: 'ios.png', by: 'image' } } }
  imageOptions.replaceDefaultImagePath(step, 'android-new.png')
  assert.equal(step.args.image_path, 'android-new.png')
  assert.equal(step.options.image_options.android, undefined)
  assert.deepEqual(step.options.image_options.ios, maps.ios)
  assert.equal(step.platform_overrides.ios.selector, 'ios.png')
})

test('changing an Android path preserves the iOS binding borrowed from the previous template', () => {
  const step = { action: 'click_image', selector: 'android-new.png', args: { image_path: 'android-old.png', image_options: { android: copy(config) } },
    platform_overrides: { android: { by: 'image', selector: 'android-old.png' } } }
  imageOptions.replaceDefaultImagePath(step, 'android-new.png')
  assert.equal(imageOptions.selectImagePath(step, 'ios'), 'android-old.png')
  assert.deepEqual(imageOptions.selectImageOptions(step, 'ios'), { template_sha256: sha, tap_anchor: config.tap_anchor })
  assert.equal(imageOptions.selectImagePath(step, 'android'), 'android-new.png')
  assert.equal(imageOptions.selectImageOptions(step, 'android'), null)
})

test('changing an Android path preserves an iOS template referenced only by args.path', () => {
  const step = { action: 'click_image', selector: 'android-new.png', args: { path: 'ios-old.png', image_options: { ios: copy(config) } } }
  imageOptions.replaceDefaultImagePath(step, 'android-new.png')
  assert.equal(imageOptions.selectImagePath(step, 'ios'), 'ios-old.png')
  assert.deepEqual(imageOptions.selectImageOptions(step, 'ios'), config)
  assert.equal(imageOptions.selectImagePath(step, 'android'), 'android-new.png')
  assert.equal(imageOptions.selectImageOptions(step, 'android'), null)
})

test('screen-edge selection preserves the original tap as a template anchor', () => {
  const selection = { x1: 0, y1: 83, x2: 65, y2: 165 }
  const anchor = imageOptions.anchorFromSelection(selection, { x: 10, y: 124 })
  assert.deepEqual(imageOptions.pointFromAnchor(selection, anchor), [10, 124])
  assert.deepEqual(imageOptions.parseSearchRegion(' [0, 0.4, 1, 0.9] '), [0, 0.4, 1, 0.9])
  for (const value of ['[0,0,0,1]', '[0,-1,1,1]', '[0,0,"1",1]', '[0,0,1]', 'no']) {
    assert.throws(() => imageOptions.parseSearchRegion(value))
  }
  assert.throws(() => imageOptions.makeRecordedImageOptions({}, anchor), /更新/)
})

function cropSetup({ platform = 'android', tapError } = {}) {
  const steps = [], taps = [], tapAttempts = [], executions = [], requests = [], crops = []
  const api = {
    ensureImageMatcher: async () => {},
    cropTemplate: async (...args) => { crops.push(args); return { data: { image_path: 'static/images/new.png', template_sha256: sha, source_size: [1080, 2400], quality: { valid: true } } } },
    matchImage: async payload => { requests.push(copy(payload)); throw new Error('recording must not require a preview request') },
    executeStep: async payload => { executions.push(copy(payload)); throw new Error('recording must not start image replay') }
  }
  const source = stripImports(read('../src/composables/useScreenCrop.js')).replace(/export /g, '')
  const messages = []
  const localNotices = Object.fromEntries(['info', 'warning', 'success', 'error'].map(level => [level, text => messages.push({ level, text })]))
  const deps = { ref, computed, nextTick, watch, api, ElMessage: localNotices, useCaseStore: () => ({ addStep: step => steps.push(copy(step)) }), getErrorDetail: (err, fallback) => err?.message || fallback, ...imageOptions }
  const useScreenCrop = new Function(...Object.keys(deps), source + '\nreturn useScreenCrop')(...Object.values(deps))
  const screenshot = ref('data:image/png;base64,SCREEN'), serial = ref('device-a')
  const crop = useScreenCrop({
    canvasRef: ref({ clientWidth: 540, clientHeight: 1200, getBoundingClientRect: () => ({ left: 0, top: 0 }) }),
    imgRef: ref({ naturalWidth: 1080, naturalHeight: 2400, getBoundingClientRect: () => ({ left: 0, top: 0, width: 540, height: 1200 }) }),
    screenshot, selectedSerial: serial, selectedPlatform: ref(platform), loading: ref(false), liveMode: ref(false), isIosLivePreview: ref(false),
    getMappedCoordinates: (x, y) => ({ canvasX: x / 2, canvasY: y / 2, realX: x, realY: y }),
    ensureInteractionReady: () => true,
    executeRawTapAndRefresh: async (x, y) => {
      tapAttempts.push([x, y])
      if (tapError) throw new Error(tapError)
      taps.push([x, y])
    },
    updateStateFromDump() {}, shouldRequestDeviceInfo: () => false
  })
  const begin = () => crop.beginQuickImageCapture({ host: 'static', node: { x1: 400, y1: 200, x2: 500, y2: 300 }, x: 410, y: 250, executeTap: true })
  return { crop, api, begin, steps, taps, tapAttempts, executions, requests, crops, screenshot, serial, messages }
}

const cropEvent = (x, y) => ({ clientX: x, clientY: y, preventDefault() {}, stopPropagation() {} })

test('quick image recording saves the anchor and submits one coordinate tap without image replay', async () => {
  const context = cropSetup({ platform: 'ios' })
  await context.begin()
  assert.equal(context.taps.length, 0)
  await context.crop.confirmQuickImageCapture()
  assert.deepEqual(context.requests, [])
  assert.equal(context.crops.length, 1)
  assert.deepEqual(context.taps, [[410, 250]])
  const anchor = context.steps[0].options.image_options.ios.tap_anchor
  assert.deepEqual(anchor, [45 / 170, 85 / 170])
  assert.deepEqual(context.executions, [])
  assert.equal(context.steps[0].options.image_options.android, undefined)
  assert.equal(context.crop.imageCropMode.value, false)
})

test('a failed recording tap retains the step and reports the error without retrying', async () => {
  const context = cropSetup({ tapError: '设备连接已断开' })
  await context.begin()
  await context.crop.confirmQuickImageCapture()
  assert.deepEqual(context.requests, [])
  assert.deepEqual(context.taps, [])
  assert.equal(context.tapAttempts.length, 1)
  assert.deepEqual(context.executions, [])
  assert.equal(context.steps.length, 1, 'the recorded step remains available to edit')
  assert.match(context.messages.find(message => message.level === 'warning').text, /设备连接已断开/)
})

test('an old Desktop stops recording before uploading or executing', async () => {
  const context = cropSetup()
  context.api.ensureImageMatcher = async () => { throw new Error('请升级桌面客户端以使用高精度图像功能') }
  await context.begin()
  await context.crop.confirmQuickImageCapture()
  assert.deepEqual(context.crops, [])
  assert.deepEqual(context.requests, [])
  assert.deepEqual(context.executions, [])
  assert.match(context.messages.find(message => message.level === 'error').text, /请升级桌面客户端/)
})

test('a cancelled or changed device capture cannot save or tap after upload completes', async () => {
  for (const cancel of [ctx => ctx.crop.cancelQuickImageCapture(), ctx => { ctx.serial.value = 'device-b' },
                        ctx => { ctx.screenshot.value = 'data:image/png;base64,NEW_SCREEN' }]) {
    const context = cropSetup()
    await context.begin()
    let resolve
    const upload = context.api.cropTemplate
    context.api.cropTemplate = (...args) => new Promise(done => { resolve = async () => done(await upload(...args)) })
    const saving = context.crop.confirmQuickImageCapture()
    for (let i = 0; i < 10 && !resolve; i++) await Promise.resolve()
    cancel(context)
    await resolve()
    await saving
    assert.deepEqual(context.steps, [])
    assert.deepEqual(context.taps, [])
  }
})

test('double confirmation submits one crop and one recording tap', async () => {
  const context = cropSetup()
  await context.begin()
  await Promise.all([context.crop.confirmQuickImageCapture(), context.crop.confirmQuickImageCapture()])
  assert.equal(context.crops.length, 1)
  assert.equal(context.tapAttempts.length, 1)
  assert.deepEqual(context.executions, [])
  assert.equal(context.steps.length, 1)
})

test('adjusting a quick crop preserves the original tap within the new selection', async () => {
  const context = cropSetup()
  await context.begin()
  context.crop.onCanvasMouseDown(cropEvent(390, 210))
  await context.crop.onCanvasMouseUp(cropEvent(490, 310))
  assert.equal(context.crops.length, 0)
  await context.crop.confirmQuickImageCapture()
  assert.deepEqual(context.steps[0].args.image_options.android.tap_anchor, [0.2, 0.4])
  assert.deepEqual(context.taps, [[410, 250]])
})

test('a new crop outside the original tap uses the new centre for recording and the saved anchor', async () => {
  const context = cropSetup()
  await context.begin()
  context.crop.onCanvasMouseDown(cropEvent(600, 400))
  await context.crop.onCanvasMouseUp(cropEvent(700, 500))
  await context.crop.confirmQuickImageCapture()
  assert.deepEqual(context.taps, [[650, 450]])
  assert.deepEqual(context.steps[0].args.image_options.android.tap_anchor, [0.5, 0.5])
})

test('an unsuccessful template save never submits a recording tap', async () => {
  const context = cropSetup()
  context.api.cropTemplate = async () => { throw new Error('上传失败') }
  await context.begin()
  await context.crop.confirmQuickImageCapture()
  assert.deepEqual(context.tapAttempts, [])
  assert.deepEqual(context.steps, [])
})

test('recording coordinates use the existing interaction route without adding another step', async () => {
  const source = read('../src/components/DeviceStage.vue')
    .match(/const executeRawTapAndRefresh = async[\s\S]*?\n}\n/)[0]
  const calls = [], updates = []
  const api = {
    interactDevice: async (...args) => { calls.push(args); return { data: { dump: { screenshot: 'NEW' } } } },
    executeStep: () => assert.fail('a recording gesture must not enter the replay runner')
  }
  const tap = new Function('api', 'hierarchyXml', 'selectedSerial', 'liveMode', 'updateStateFromDump',
    source + '\nreturn executeRawTapAndRefresh')(api, ref('XML'), ref('ios-device'), ref(false), dump => updates.push(dump))
  await tap(410, 250)
  assert.deepEqual(calls, [[410, 250, 'click', 'XML', null, 'ios-device', false, true]])
  assert.deepEqual(updates, [{ screenshot: 'NEW' }])
})

for (const action of ['click_image', 'assert_image']) {
  for (const matchMode of action === 'assert_image' ? ['exists', 'not_exists'] : ['exists']) {
    test(`${action}/${matchMode} recapture saves on mouse-up without preview or device action`, async () => {
      const context = cropSetup()
      const step = { uuid: 'saved', action, selector: 'old.png', args: { image_path: 'old.png', match_mode: matchMode }, options: { image_options: copy(maps), match_mode: matchMode } }
      await context.crop.startImageCrop(step)
      context.crop.onCanvasMouseDown(cropEvent(365, 165))
      await context.crop.onCanvasMouseUp(cropEvent(535, 335))
      assert.equal(step.selector, 'static/images/new.png')
      assert.equal(step.action, action)
      assert.equal(step.args.match_mode, matchMode)
      assert.equal(step.options.match_mode, matchMode)
      assert.deepEqual(step.options.image_options.android.tap_anchor, [0.5, 0.5])
      assert.equal(step.options.image_options.android.template_sha256, sha)
      assert.deepEqual(step.options.image_options.ios, maps.ios)
      assert.deepEqual(context.executions, [])
      assert.deepEqual(context.tapAttempts, [])
      assert.deepEqual(context.requests, [])
      assert.equal(context.crop.imageCropMode.value, false)
    })
  }
}

test('changing the edited step during upload cannot overwrite its new action', async () => {
  const context = cropSetup()
  const step = reactive({ uuid: 'saved', action: 'click_image', selector: 'old.png', args: {}, options: {} })
  await context.crop.startImageCrop(step)
  let resolve
  const upload = context.api.cropTemplate
  context.api.cropTemplate = (...args) => new Promise(done => { resolve = async () => done(await upload(...args)) })
  context.crop.onCanvasMouseDown(cropEvent(365, 165))
  const saving = context.crop.onCanvasMouseUp(cropEvent(535, 335))
  for (let i = 0; i < 10 && !resolve; i++) await Promise.resolve()
  step.action = 'assert_text'
  step.selector = ''
  step.options = {}
  await resolve()
  await saving
  assert.equal(step.selector, '')
  assert.deepEqual(context.taps, [])
  assert.equal(context.crop.imageCropMode.value, false)
})

function editorSetup(api = {}, props = { deviceSerial: 'ios-device' }) {
  const source = stripImports(read('../src/components/StepBuilder.vue').match(/<script setup>([\s\S]*?)<\/script>/)[1])
  const deps = {
    ref, nextTick, onMounted() {}, watch, defineProps: () => props, defineEmits: () => () => {},
    useCaseStore: () => ({}), storeToRefs: () => ({ currentCase: ref({ steps: [] }), lastAddedStepUuid: ref(null) }),
    api, apiClient: api, ElMessage: notices, createUuid: () => 'generated', ...actionConstants, ...imageOptions
  }
  const scope = effectScope()
  const editor = scope.run(() => new Function(...Object.keys(deps), source + '\nreturn {buildExecutableStep,previewImageMatch,imagePreviewResult,imagePreviewBusy,imagePreviewVisible}')(...Object.values(deps)))
  return { ...editor, dispose: () => scope.stop() }
}

// Recorded case 48 steps use one iOS template on Android, with an explicit
// adaptive profile. The store adds options.image_match before Pinia proxies it.
function recordedImageSteps() {
  return [
    { order: 6, action: 'assert_image', image: 'ddcefbc8a6fbc8e6',
      sha: 'ddcefbc8a6fbc8e6cd6ecc382757c505ef721c756e951bca974ea9e40e75dbb5', anchor: [0.5, 0.5] },
    { order: 8, action: 'click_image', image: '9873a10dbe180617',
      sha: '9873a10dbe18061710badde2ffde31d19ac0a91e536c63f13d2d9aa6a7d471a9', anchor: [0.48598130841121495, 0.5135135135135135] }
  ].map(({ order, action, image, sha, anchor }) => {
    const imagePath = `static/images/element_${image}.png`
    return { order, action,
      args: { image_path: imagePath, image_options: { ios: { template_sha256: sha, tap_anchor: anchor, source_size: [1178, 2556] } },
        image_match: { profile: 'adaptive-v2' }, ...(action === 'assert_image' ? { match_mode: 'exists' } : {}) },
      value: '', execute_on: ['android', 'ios'],
      platform_overrides: { android: { selector: imagePath, by: 'image' }, ios: { selector: imagePath, by: 'image' } },
      timeout: 10, error_strategy: 'ABORT', retry_count: 0, description: '' }
  })
}

function previewApi(result = { status: 'matched', candidates: [{ accepted: true }] }) {
  const requests = []
  return { requests, api: {
    ensureImageMatcher: async () => { requests.push({ stage: 'capability' }) },
    getDeviceDump: async serial => {
      requests.push({ stage: 'screenshot', serial })
      return { data: { screenshot: 'SCREEN', device_info: { platform: 'android' } } }
    },
    matchImage: async payload => { requests.push({ stage: 'match', payload }); return { data: result } },
    executeStep: () => assert.fail('preview must not execute a device step'),
    interactDevice: () => assert.fail('preview must not tap a device')
  } }
}

test('shared image descriptors deeply clone Vue proxies while preserving canonical metadata and conflict checks', () => {
  const descriptor = { schema_version: 1, template_size: [277, 122],
    regions: [{ id: 'visual-1', kind: 'visual', rect: [0, 0, 0.3, 1], required: true },
      { id: 'text-1', kind: 'text', rect: [0.4, 0, 1, 1], text: '云闪付', required: true }],
    relations: [{ visual: 'visual-1', text: 'text-1', direction: 'right' }], tap: { region_id: 'visual-1', anchor: [0.2, 0.7] } }
  const shared = { profile: 'adaptive-v2', template_sha256: sha, descriptor,
    descriptor_canonical: JSON.stringify(descriptor), descriptor_sha256: 'c'.repeat(64) }
  const step = reactive({ args: { image_match: copy(shared) }, options: { image_match: copy(shared) } })
  assert.equal(isProxy(step.args.image_match.descriptor.regions), true)
  const cloned = imageOptions.getSharedImageMatch(step)
  assert.deepEqual(cloned, shared)
  assert.doesNotThrow(() => structuredClone(cloned))
  cloned.descriptor.regions[1].text = 'changed'
  cloned.descriptor.tap.anchor[0] = 0.9
  assert.equal(step.args.image_match.descriptor.regions[1].text, '云闪付')
  assert.equal(step.options.image_match.descriptor.tap.anchor[0], 0.2)
  assert.equal(step.args.image_match.descriptor_canonical, JSON.stringify(descriptor))
  const mixed = { args: { image_match: { ...shared, descriptor: reactive(copy(descriptor)) } } }
  assert.deepEqual(imageOptions.getSharedImageMatch(mixed), shared, 'nested proxies inside a plain config are cloned too')
  step.options.image_match.descriptor.regions[1].text = 'different'
  assert.throws(() => imageOptions.getSharedImageMatch(step), /图像匹配配置冲突/)
})

test('reactive shared settings survive save and recapture without aliasing their descriptor', () => {
  const { standardStepToUiStep, uiStepToStandardStep } = storeFunctions()
  const step = reactive(standardStepToUiStep(recordedImageSteps()[0]))
  const descriptor = { schema_version: 1, template_size: [303, 294],
    regions: [{ id: 'whole', kind: 'visual', rect: [0, 0, 1, 1], required: true }],
    relations: [], tap: { region_id: 'whole', anchor: [0.5, 0.5] } }
  const shared = { profile: 'adaptive-v2', template_sha256: config.template_sha256,
    descriptor, descriptor_canonical: JSON.stringify(descriptor), descriptor_sha256: 'c'.repeat(64) }
  const incoming = reactive(copy(shared))
  imageOptions.setSharedImageMatch(step, incoming)
  incoming.descriptor.regions[0].rect[0] = 0.2
  assert.deepEqual(step.args.image_match, shared)
  assert.deepEqual(step.options.image_match, shared)
  const saved = uiStepToStandardStep(step, 6)
  assert.deepEqual(saved.args.image_match, shared)
  saved.args.image_match.descriptor.tap.anchor[0] = 0.1
  assert.equal(step.options.image_match.descriptor.tap.anchor[0], 0.5)

  // Re-capture a template shared by both platforms, retaining its bound
  // descriptor and canonical digest through the same reactive draft.
  imageOptions.bindRecordedImage(step, 'ios', step.selector, copy(config), reactive(copy(shared)))
  const recaptured = uiStepToStandardStep(step, 6)
  assert.deepEqual(recaptured.args.image_match, shared)
  assert.deepEqual(recaptured.args.image_options.ios, config)
  assert.deepEqual(imageOptions.getSharedImageMatch(step), shared)
})

test('loaded recorded image assertions and clicks preview through real Vue reactive watchers', async t => {
  const { standardStepToUiStep } = storeFunctions()
  for (const standard of recordedImageSteps()) {
    const step = reactive(standardStepToUiStep(copy(standard)))
    assert.equal(isProxy(step.options.image_match), true)
    const { api, requests } = previewApi()
    const editor = editorSetup(api, reactive({ deviceSerial: 'android-device' }))
    t.after(editor.dispose)
    await editor.previewImageMatch(step)
    assert.equal(editor.imagePreviewBusy.value, false)
    assert.equal(editor.imagePreviewResult.value.status, 'matched')
    assert.deepEqual(requests.map(request => request.stage), ['capability', 'screenshot', 'match'])
    const payload = requests[2].payload
    assert.equal(payload.image_path, standard.args.image_path)
    assert.deepEqual(payload.image_match, { profile: 'adaptive-v2' })
    assert.deepEqual(payload.image_options, { template_sha256: standard.args.image_options.ios.template_sha256,
      tap_anchor: standard.args.image_options.ios.tap_anchor })
    assert.deepEqual(step.args, standard.args, 'preview must not mutate saved settings')
  }
  const legacy = copy(recordedImageSteps()[0])
  delete legacy.args.image_match
  const step = reactive(standardStepToUiStep(legacy))
  assert.deepEqual(step.options.image_match, { profile: 'adaptive-v2' })
  const { api } = previewApi()
  const editor = editorSetup(api, { deviceSerial: 'android-device' })
  t.after(editor.dispose)
  await editor.previewImageMatch(step)
  assert.equal(editor.imagePreviewBusy.value, false)
  assert.equal(editor.imagePreviewResult.value.status, 'matched')
})

test('invalid preview configuration reports an error, releases loading, and permits a corrected retry', async t => {
  const step = reactive({ action: 'assert_image', selector: 'target.png', options: { image_match: { profile: 'invalid-profile' } } })
  const { api, requests } = previewApi()
  const editor = editorSetup(api, { deviceSerial: 'android-device' })
  t.after(editor.dispose)
  await assert.doesNotReject(editor.previewImageMatch(step))
  assert.equal(editor.imagePreviewBusy.value, false)
  assert.match(editor.imagePreviewResult.value.reason, /图像匹配配置版本无效/)
  assert.equal(requests.length, 0)
  assert.doesNotThrow(() => { step.options.image_match.profile = 'adaptive-v2' })
  await editor.previewImageMatch(step)
  assert.equal(editor.imagePreviewBusy.value, false)
  assert.equal(editor.imagePreviewResult.value.status, 'matched')
})

test('an initial preview key exception is handled before loading can become stuck', async t => {
  const step = reactive({ action: 'click_image', selector: 'target.png', options: {}, platform_overrides: {} })
  step.platform_overrides.cycle = step.platform_overrides
  const { api } = previewApi()
  const editor = editorSetup(api, { deviceSerial: 'android-device' })
  t.after(editor.dispose)
  await assert.doesNotReject(editor.previewImageMatch(step))
  assert.equal(editor.imagePreviewBusy.value, false)
  assert.equal(editor.imagePreviewResult.value.status, 'unsupported')
  assert.ok(editor.imagePreviewResult.value.reason)
  delete step.platform_overrides.cycle
  await editor.previewImageMatch(step)
  assert.equal(editor.imagePreviewBusy.value, false)
  assert.equal(editor.imagePreviewResult.value.status, 'matched')
})

test('a rejected matching request releases preview loading and allows another request', async t => {
  const step = reactive(storeFunctions().standardStepToUiStep(recordedImageSteps()[0]))
  const { api } = previewApi()
  let attempts = 0
  api.matchImage = async () => {
    if (++attempts === 1) throw new Error('定位验证请求超时')
    return { data: { status: 'matched', candidates: [{ accepted: true }] } }
  }
  const editor = editorSetup(api, { deviceSerial: 'android-device' })
  t.after(editor.dispose)
  await editor.previewImageMatch(step)
  assert.equal(editor.imagePreviewBusy.value, false)
  assert.match(editor.imagePreviewResult.value.reason, /定位验证请求超时/)
  await editor.previewImageMatch(step)
  assert.equal(attempts, 2)
  assert.equal(editor.imagePreviewBusy.value, false)
  assert.equal(editor.imagePreviewResult.value.status, 'matched')
})

test('configuration changes while a preview is pending do not throw from its synchronous watcher', async t => {
  const step = reactive(storeFunctions().standardStepToUiStep(recordedImageSteps()[1]))
  const { api } = previewApi()
  let finishMatch
  api.matchImage = () => new Promise(resolve => { finishMatch = resolve })
  const editor = editorSetup(api, { deviceSerial: 'android-device' })
  t.after(editor.dispose)
  const pending = editor.previewImageMatch(step)
  pending.catch(() => {})
  for (let i = 0; i < 10 && !finishMatch; i++) await Promise.resolve()
  assert.equal(typeof finishMatch, 'function')
  assert.doesNotThrow(() => { step.options.image_match.profile = 'invalid-profile' })
  finishMatch({ data: { status: 'matched', candidates: [{ accepted: true }] } })
  await pending
  assert.equal(editor.imagePreviewBusy.value, false)
  assert.equal(editor.imagePreviewResult.value.status, 'unstable')
  assert.deepEqual(editor.imagePreviewResult.value.candidates, [])
  assert.doesNotThrow(() => { step.options.image_match.profile = 'adaptive-v2' })
  api.matchImage = async () => ({ data: { status: 'matched', candidates: [{ accepted: true }] } })
  await editor.previewImageMatch(step)
  assert.equal(editor.imagePreviewBusy.value, false)
  assert.equal(editor.imagePreviewResult.value.status, 'matched')
})

test('step editor preserves both image actions and their metadata without preview requests', () => {
  const editor = editorSetup()
  for (const action of ['click_image', 'assert_image']) {
    const step = { uuid: 'step', action, selector: 'android.png', args: { image_path: 'stale.png', match_mode: 'not_exists' },
      options: { image_options: copy(maps), match_mode: 'not_exists' }, platform_overrides: { android: { by: 'image', selector: 'android.png' }, ios: { by: 'image', selector: 'ios.png' } } }
    const executable = editor.buildExecutableStep(step)
    assert.equal(executable.action, action)
    assert.equal(executable.args.image_path, 'android.png')
    assert.deepEqual(executable.args.image_options, maps)
    assert.equal(executable.platform_overrides.ios.selector, 'ios.png')
    if (action === 'assert_image') assert.equal(executable.args.match_mode, 'not_exists')
  }
})

test('optional preview uses the current platform template for both image actions and never executes', async () => {
  for (const action of ['click_image', 'assert_image']) {
    const requests = []
    const result = { status: 'matched', candidates: [{ accepted: true }] }
    const api = {
      ensureImageMatcher: async () => {},
      getDeviceDump: async serial => {
        assert.equal(serial, 'ios-device')
        return { data: { screenshot: 'SCREEN', device_info: { platform: 'ios' } } }
      },
      matchImage: async payload => { requests.push(payload); return { data: result } },
      executeStep: () => { assert.fail('preview must never execute a device step') },
      interactDevice: () => { assert.fail('preview must never tap a device') }
    }
    const editor = editorSetup(api)
    const step = { action, selector: 'android.png', options: { image_options: copy(maps) },
      platform_overrides: { android: { by: 'image', selector: 'android.png' }, ios: { by: 'image', selector: 'ios.png' } } }
    await editor.previewImageMatch(step)
    assert.deepEqual(requests, [{ screenshot_base64: 'SCREEN', image_path: 'ios.png', image_options: maps.ios, image_match: { profile: 'adaptive-v2' } }])
    assert.deepEqual(editor.imagePreviewResult.value, result)
  }
})

test('a device change invalidates an optional preview already in flight', async () => {
  const props = reactive({ deviceSerial: 'ios-device' })
  let resolve
  const api = {
    ensureImageMatcher: async () => {},
    getDeviceDump: async () => ({ data: { screenshot: 'SCREEN', device_info: { platform: 'ios' } } }),
    matchImage: () => new Promise(done => { resolve = done })
  }
  const editor = editorSetup(api, props)
  const pending = editor.previewImageMatch({ action: 'assert_image', selector: 'target.png', options: {} })
  for (let i = 0; i < 10 && !resolve; i++) await Promise.resolve()
  props.deviceSerial = 'other-device'
  resolve({ data: { status: 'matched', candidates: [{ accepted: true }] } })
  await pending
  assert.equal(editor.imagePreviewResult.value.status, 'unstable')
  assert.deepEqual(editor.imagePreviewResult.value.candidates, [])
})

test('assertion preview treats duplicate verified targets as present', () => {
  const result = { status: 'ambiguous', candidates: [{ accepted: true }, { accepted: false }, { accepted: true }] }
  assert.match(imageOptions.imageMatchPreviewText(result, 'assert_image'), /2 个合格目标，图像存在/)
  assert.match(imageOptions.imageMatchPreviewText(result, 'click_image'), /多个相似目标/)
  assert.match(imageOptions.imageMatchPreviewText({ status: 'unsupported' }, 'assert_image'), /请升级/)
})

test('image assertion errors retain existence meaning when several matches are present', () => {
  const error = '断言失败：页面存在通过严格验证的图像目标'
  const result = { error, output: { image_match: { status: 'ambiguous', candidates: [{ accepted: true }, { accepted: true }] } } }
  assert.equal(imageOptions.imageExecutionFailure(result, 'assert_image'), error)
  assert.match(imageOptions.imageExecutionFailure(result, 'click_image'), /多个相似目标/)
})
