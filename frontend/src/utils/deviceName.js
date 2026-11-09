/**
 * 设备展示名统一口径。
 *
 * 展示优先级：用户改名 custom_name > 市场型号 market_name > model > 序列号兜底。
 * market_name 在 Android 上是厂商市场型号（如「OPPO Reno14 Pro」，model 是 PKZ110）；
 * iOS 侧 model 已是市场名（如 iPhone 14 Pro），云端不再往 market_name 写手机名。
 */
export function deviceDisplayName(device, fallback = '') {
  if (!device) return fallback
  return device.custom_name || device.market_name || device.model || device.serial || fallback
}

// 形似序列号/UDID 的标识（iOS UDID 8-16 位、40 位；安卓 serial 类似）：不能当名字展示
const SERIAL_LIKE = /^[0-9A-Za-z-]{8,}$/
// 桌面客户端历史执行记录 device_info 写的是「桌面客户端 · <序列号>」，不是型号
const LEGACY_CLIENT_INFO_PREFIX = '桌面客户端 · '

/**
 * 报告行的设备展示名：查设备表 → 历史 device_info（含「Model (serial)」旧格式）→ 兜底。
 *
 * 设备已删除 / 查不到时把序列号（尤其 iOS UDID）当名字会一串编号，返回 fallback。
 */
export function deviceNameFromRecord(deviceSerial, device, fallbackInfo, fallback = '未知设备') {
  const name = deviceDisplayName(device, '')
  if (name) return name

  let cleaned = String(fallbackInfo || '').trim()
  cleaned = cleaned.replace(/\s*\([^)]+\)$/, '').trim()
  if (cleaned.startsWith(LEGACY_CLIENT_INFO_PREFIX)) {
    cleaned = cleaned.slice(LEGACY_CLIENT_INFO_PREFIX.length).trim()
  }
  if (cleaned && !SERIAL_LIKE.test(cleaned)) return cleaned

  // device_serial 历史上也可能存着「名字 (序列号)」整体
  const serial = String(deviceSerial || '').replace(/\s*\([^)]+\)$/, '').trim()
  if (!serial) return fallback
  return SERIAL_LIKE.test(serial) ? fallback : serial
}
