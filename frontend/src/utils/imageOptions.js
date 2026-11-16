// Image settings belong to a template fingerprint, independently for each platform.
export const IMAGE_OPTION_PLATFORMS = ['android', 'ios', 'default']

export function imageMatchStatusText(result) {
  const labels = { matched: '已找到目标', not_found: '当前画面未找到目标',
    ambiguous: '存在多个相似目标，请重新圈选更有区分度的模板', invalid_template: '模板特征不足，请重新框选',
    unstable: '目标位置尚未稳定，请重试', unsupported: '当前客户端不支持高精度图像匹配，请升级' }
  const reasons = {
    template_too_small: '选区过小，请扩大到完整目标',
    constant_template: '选区接近纯色，请包含目标轮廓或文字',
    no_foreground: '选区缺少可区分的轮廓，请缩小背景并包含目标',
    template_hash_mismatch: '模板已更换，请重新截取以更新模板信息',
    aspect_ratio_changed: '当前画面方向或宽高比与录制不一致，请重新截取对应模板',
    deadline_exceeded: '图像定位超时，请检查当前画面或重新截取模板'
  }
  if (!result) return '请圈选完整的目标图像'
  const reason = String(result.reason || '')
  const text = reasons[reason] || (/[\u3400-\u9fff]/.test(reason) ? reason : labels[result.status]) || '定位验证失败'
  const warnings = { low_contrast: '选区对比度较低', sparse_features: '选区可区分特征较少', dense_texture: '选区纹理过密' }
  const warningText = (result.quality?.warnings || []).map(value => warnings[value]).filter(Boolean).join('，')
  return warningText ? `${text}；${warningText}` : text
}

export function imageExecutionFailure(result, action = 'click_image') {
  // Assertion failures must explain exists/not_exists, even for duplicate targets.
  if (action === 'assert_image' && /[\u3400-\u9fff]/.test(String(result?.error || ''))) return result.error
  const diagnostic = result?.output?.image_match
  if (diagnostic && diagnostic.status !== 'matched') return imageMatchStatusText(diagnostic)
  const error = String(result?.error || '')
  return /[\u3400-\u9fff]/.test(error) ? error : `${action === 'assert_image' ? '图像断言' : '图像点击'}失败，请检查当前画面或重新截取模板`
}

export function cloneImageOptions(value) {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? JSON.parse(JSON.stringify(value)) : {}
}

export function getImageOptionsMap(step) {
  const options = step?.options
  const value = options && Object.hasOwn(options, 'image_options')
    ? options.image_options : step?.args?.image_options
  return cloneImageOptions(value)
}

export function selectImageOptions(step, platform) {
  const options = getImageOptionsMap(step)
  const selected = options[platform] || options.default
  if (Object.keys(options).length && !selected) throw new Error('此设备平台尚未配置图像模板，请在该平台重新截取')
  return selected || null
}

export function setImageOptionsMap(step, map) {
  step.options = { ...(step.options || {}), image_options: cloneImageOptions(map) }
  step.args = { ...(step.args || {}), image_options: cloneImageOptions(map) }
}

export function clearImageOptions(step) {
  // An explicit empty map also overrides any args retained from a saved step.
  setImageOptionsMap(step, {})
}

export function bindRecordedImage(step, platform, imagePath, settings) {
  const map = getImageOptionsMap(step)
  const overrides = { ...(step.platform_overrides || {}) }
  const other = platform === 'ios' ? 'android' : 'ios'
  const oldPath = overrides[other]?.selector || step.args?.image_path || step.selector
  if (oldPath && !overrides[other]) overrides[other] = { selector: oldPath, by: 'image' }
  if (oldPath && map.default && !map[other]) map[other] = cloneImageOptions(map.default)
  delete map.default
  map[platform] = settings
  overrides[platform] = { selector: imagePath, by: 'image' }
  step.platform_overrides = overrides
  // The legacy selector is the Android/default path; an iOS recrop must not replace it.
  step.selector = overrides.android?.selector || imagePath
  step.selector_type = 'image'
  step.args = { ...(step.args || {}), image_path: step.selector }
  setImageOptionsMap(step, map)
}

export function replaceDefaultImagePath(step, imagePath) {
  const map = getImageOptionsMap(step)
  const overrides = { ...(step.platform_overrides || {}) }
  const iosPath = overrides.ios?.selector || step.args?.image_path
  if (iosPath && (map.ios || map.default)) {
    overrides.ios = { selector: iosPath, by: 'image' }
    if (!map.ios && map.default) map.ios = cloneImageOptions(map.default)
  }
  delete map.android
  delete map.default
  step.selector = imagePath
  step.args = { ...(step.args || {}), image_path: imagePath }
  step.platform_overrides = { ...overrides, android: { selector: imagePath, by: 'image' } }
  setImageOptionsMap(step, map)
}

export function anchorFromSelection(selection, point) {
  const width = selection.x2 - selection.x1
  const height = selection.y2 - selection.y1
  if (width <= 0 || height <= 0) throw new Error('框选区域无效')
  return [
    Math.max(0, Math.min(1, (point.x - selection.x1) / width)),
    Math.max(0, Math.min(1, (point.y - selection.y1) / height))
  ]
}

export function pointFromAnchor(selection, anchor = [0.5, 0.5]) {
  return [
    selection.x1 + (selection.x2 - selection.x1) * anchor[0],
    selection.y1 + (selection.y2 - selection.y1) * anchor[1]
  ]
}

export function parseSearchRegion(text) {
  if (!String(text || '').trim()) return null
  let region
  try { region = JSON.parse(text) } catch { throw new Error('搜索区域格式为 [左, 上, 右, 下]，取值 0–1') }
  if (!Array.isArray(region) || region.length !== 4
    || region.some(value => typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1)
    || region[0] >= region[2] || region[1] >= region[3]) {
    throw new Error('搜索区域须满足 0 ≤ 左 < 右 ≤ 1，0 ≤ 上 < 下 ≤ 1')
  }
  return region
}

export function makeRecordedImageOptions(metadata, anchor) {
  if (!/^[a-f0-9]{64}$/i.test(String(metadata?.template_sha256 || ''))
    || !Array.isArray(metadata?.source_size) || metadata.source_size.length !== 2
    || metadata.source_size.some(value => !Number.isFinite(value) || value <= 0)) {
    throw new Error('当前客户端未返回模板校验信息，请更新后重新截取')
  }
  if (!Array.isArray(anchor) || anchor.length !== 2
    || anchor.some(value => !Number.isFinite(value) || value < 0 || value > 1)) {
    throw new Error('点击位置须在模板范围内，取值 0–1')
  }
  return {
    template_sha256: metadata.template_sha256,
    tap_anchor: [...anchor],
    source_size: [...metadata.source_size]
  }
}
