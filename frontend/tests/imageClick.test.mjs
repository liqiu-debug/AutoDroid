import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { ref, computed, nextTick, watch, reactive } from 'vue'
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

test('an explicitly cleared template binding cannot resurrect old args metadata', () => {
  const { uiStepToStandardStep } = storeFunctions()
  const step = { action: 'click_image', selector: 'static/images/new.png', selector_type: 'image', args: { image_options: copy(maps) }, options: {} }
  imageOptions.clearImageOptions(step)
  assert.deepEqual(uiStepToStandardStep(step, 1).args.image_options, {})
  assert.equal(imageOptions.selectImageOptions(step, 'ios'), null)
  const onlyAndroid = { options: { image_options: { android: config } } }
  assert.throws(() => imageOptions.selectImageOptions(onlyAndroid, 'ios'), /平台尚未配置/, 'never borrow Android metadata for an iOS template')
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

function cropSetup({ match, platform = 'android' } = {}) {
  const steps = [], taps = [], executions = [], requests = [], crops = []
  const api = {
    ensureImageMatcher: async () => {},
    cropTemplate: async (...args) => { crops.push(args); return { data: { image_path: 'static/images/new.png', template_sha256: sha, source_size: [1080, 2400], quality: { valid: true } } } },
    matchImage: async payload => {
      requests.push(copy(payload))
      return { data: match ? await match(payload) : { status: 'matched', matcher_version: 'test', point: [410, 250], candidates: [{ rect: [365, 165, 170, 170], point: [410, 250], similarity: 1, accepted: true }] } }
    }
  }
  const source = stripImports(read('../src/composables/useScreenCrop.js')).replace(/export /g, '')
  const deps = { ref, computed, nextTick, watch, api, ElMessage: notices, useCaseStore: () => ({ addStep: step => steps.push(copy(step)) }), getErrorDetail: (err, fallback) => err?.message || fallback, ...imageOptions }
  const useScreenCrop = new Function(...Object.keys(deps), source + '\nreturn useScreenCrop')(...Object.values(deps))
  const screenshot = ref('data:image/png;base64,SCREEN'), serial = ref('device-a')
  const crop = useScreenCrop({
    canvasRef: ref({ clientWidth: 540, clientHeight: 1200, getBoundingClientRect: () => ({ left: 0, top: 0 }) }),
    imgRef: ref({ naturalWidth: 1080, naturalHeight: 2400, getBoundingClientRect: () => ({ left: 0, top: 0, width: 540, height: 1200 }) }),
    screenshot, selectedSerial: serial, selectedPlatform: ref(platform), loading: ref(false), liveMode: ref(false), isIosLivePreview: ref(false),
    getMappedCoordinates: (x, y) => ({ canvasX: x / 2, canvasY: y / 2, realX: x, realY: y }),
    ensureInteractionReady: () => true,
    executeImageStepAndRefresh: async step => {
      executions.push(copy(step))
      const [u, v] = step.args.image_options[platform].tap_anchor
      taps.push([Math.round(365 + 170 * u), Math.round(165 + 170 * v)])
    },
    updateStateFromDump() {}, shouldRequestDeviceInfo: () => false
  })
  const begin = () => crop.beginQuickImageCapture({ host: 'static', node: { x1: 400, y1: 200, x2: 500, y2: 300 }, x: 410, y: 250, executeTap: true })
  return { crop, api, begin, steps, taps, executions, requests, crops, screenshot, serial }
}

test('recording is click-free until validated and saves the same anchor used by the actual click', async () => {
  const context = cropSetup({ platform: 'ios' })
  await context.begin()
  await context.crop.confirmQuickImageCapture()
  assert.equal(context.steps.length, 0)
  assert.equal(context.taps.length, 0)
  assert.equal(await context.crop.validateImageSelection(), true)
  assert.equal(context.taps.length, 0, 'validation only locates')
  assert.equal(context.crop.canConfirmImage.value, true)
  const anchor = context.requests[0].image_options.tap_anchor
  assert.equal(anchor[0], 45 / 170)
  await context.crop.confirmQuickImageCapture()
  assert.deepEqual(context.taps, [[410, 250]])
  assert.deepEqual(context.steps[0].options.image_options.ios.tap_anchor, anchor)
  assert.deepEqual(context.executions[0].args.image_options.ios, context.steps[0].options.image_options.ios, 'recording clicks execute the same guarded image step as replay')
  assert.equal(context.steps[0].options.image_options.android, undefined)
})

test('ambiguous targets prevent recording and clicks even when the user confirms', async () => {
  const context = cropSetup({ match: () => ({ status: 'ambiguous', point: null, candidates: [{ rect: [0, 0, 10, 10] }, { rect: [40, 0, 10, 10] }] }) })
  await context.begin()
  assert.equal(await context.crop.validateImageSelection(), false)
  await context.crop.confirmQuickImageCapture()
  assert.deepEqual(context.taps, [])
  assert.deepEqual(context.steps, [])
})

test('an old Desktop stops validation before crop uploads or preview requests', async () => {
  const context = cropSetup()
  context.api.ensureImageMatcher = async () => { throw new Error('请升级桌面客户端以使用高精度图像功能') }
  await context.begin()
  assert.equal(await context.crop.validateImageSelection(), false)
  assert.deepEqual(context.crops, [])
  assert.deepEqual(context.requests, [])
  assert.equal(context.crop.canConfirmImage.value, false)
  assert.match(context.crop.imageMatchResult.value.reason, /请升级桌面客户端/)
})

test('anchor or screenshot edits invalidate a passing preview and pending old responses', async () => {
  const context = cropSetup()
  await context.begin()
  await context.crop.validateImageSelection()
  context.crop.setImageAnchor(0, 0.8)
  assert.equal(context.crop.canConfirmImage.value, false)
  assert.equal(context.crop.imageMatchResult.value, null)
  await context.crop.validateImageSelection()
  context.screenshot.value = 'data:image/png;base64,NEW_SCREEN'
  assert.equal(context.crop.canConfirmImage.value, false)

  let resolve
  context.api.matchImage = () => new Promise(done => { resolve = done })
  const validating = context.crop.validateImageSelection()
  for (let i = 0; i < 10 && !resolve; i++) await Promise.resolve()
  context.crop.cancelQuickImageCapture()
  resolve({ data: { status: 'matched', point: [410, 250], candidates: [] } })
  assert.equal(await validating, false)
  assert.equal(context.crop.canConfirmImage.value, false)
  assert.equal(context.crop.imageMatchResult.value, null)
  assert.deepEqual(context.taps, [])
})

test('ordinary image recapture waits for validation, then binds metadata to the selected platform', async () => {
  const context = cropSetup()
  const step = { uuid: 'saved', action: 'click_image', selector: 'old.png', args: { image_path: 'old.png' }, options: { image_options: copy(maps) } }
  await context.crop.startImageCrop(step)
  const event = (x, y) => ({ clientX: x, clientY: y, preventDefault() {}, stopPropagation() {} })
  context.crop.onCanvasMouseDown(event(365, 165))
  await context.crop.onCanvasMouseUp(event(535, 335))
  assert.equal(step.selector, 'old.png', 'mouse-up must not silently replace the saved template')
  await context.crop.validateImageSelection()
  await context.crop.confirmQuickImageCapture()
  assert.equal(step.selector, 'static/images/new.png')
  assert.equal(step.options.image_options.android.template_sha256, sha)
  assert.deepEqual(step.options.image_options.ios, maps.ios)
  assert.deepEqual(context.taps, [])
})

test('changing the edited step invalidates crop confirmation instead of overwriting the new action', async () => {
  const context = cropSetup()
  const step = reactive({ uuid: 'saved', action: 'click_image', selector: 'old.png', args: {}, options: {} })
  await context.crop.startImageCrop(step)
  const event = (x, y) => ({ clientX: x, clientY: y, preventDefault() {}, stopPropagation() {} })
  context.crop.onCanvasMouseDown(event(365, 165))
  await context.crop.onCanvasMouseUp(event(535, 335))
  await context.crop.validateImageSelection()
  assert.equal(context.crop.canConfirmImage.value, true)
  step.action = 'assert_text'
  step.selector = ''
  step.options = {}
  assert.equal(context.crop.canConfirmImage.value, false)
  await context.crop.confirmQuickImageCapture()
  assert.equal(step.selector, '')
  assert.equal(context.crop.imageCropMode.value, false)
})

test('step-editor preview selects the current device platform and only calls screenshot/match APIs', async () => {
  const step = reactive({ uuid: 'saved', action: 'click_image', selector: 'android.png', options: { image_options: copy(maps) }, args: { image_path: 'old-args.png' }, platform_overrides: { android: { selector: 'android.png', by: 'image' }, ios: { selector: 'ios.png', by: 'image' } } })
  const calls = []
  const api = {
    ensureImageMatcher: async () => {},
    getDeviceDump: async () => { calls.push('dump'); return { data: { screenshot: 'SCREEN', screenshot_format: 'png', device_info: { platform: 'ios' } } } },
    matchImage: async payload => { calls.push(copy(payload)); return { data: { status: 'matched', point: [10, 20], candidates: [] } } },
    executeStep: () => { throw new Error('preview must never execute a step') }
  }
  const source = stripImports(read('../src/components/StepBuilder.vue').match(/<script setup>([\s\S]*?)<\/script>/)[1])
  const deps = {
    ref, nextTick, onMounted() {}, watch, defineProps: () => ({ deviceSerial: 'ios-device' }), defineEmits: () => () => {},
    useCaseStore: () => ({}), storeToRefs: () => ({ currentCase: ref({ steps: [step] }), lastAddedStepUuid: ref(null) }),
    api, apiClient: api, ElMessage: notices, createUuid: () => 'generated', ...actionConstants, ...imageOptions
  }
  const editor = new Function(...Object.keys(deps), source + '\nreturn {previewImageMatch,buildExecutableStep,imagePreviewResult,updateImageAnchor}')(...Object.values(deps))
  const executable = editor.buildExecutableStep(step)
  assert.equal(executable.args.image_path, 'android.png', 'current edited path replaces a stale saved args path')
  assert.deepEqual(executable.args.image_options, maps)
  await editor.previewImageMatch(step)
  assert.equal(calls.length, 2)
  assert.equal(calls[1].image_path, 'ios.png')
  assert.deepEqual(calls[1].image_options, maps.ios)
  assert.equal(editor.imagePreviewResult.value.status, 'matched')
  editor.updateImageAnchor(step, 0, .8)
  assert.equal(editor.imagePreviewResult.value.status, 'unstable', 'an already-visible passing preview cannot survive metadata edits')
  delete step.options.image_options.ios
  calls.length = 0
  await editor.previewImageMatch(step)
  assert.equal(calls.length, 1, 'missing platform metadata must not call legacy match without config')
  assert.equal(editor.imagePreviewResult.value.status, 'unsupported')
  assert.match(editor.imagePreviewResult.value.reason, /平台尚未配置/)
})
