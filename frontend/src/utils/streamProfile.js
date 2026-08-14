const NETWORK_SERIAL_RE = /^[\w.\-]+:\d+$/

export function isNetworkDeviceSerial(serial) {
  return NETWORK_SERIAL_RE.test(String(serial || ''))
}

/**
 * 网络设备总是从服务端低延迟默认档起播。历史 localStorage 只会保留旧高清
 * 覆盖，平台重启后若自动恢复会立即重新引入链路拥塞；用户当前会话中手动
 * 选择的档位已在服务端内存中，无需由浏览器再次下发。
 */
export function shouldRestoreStoredStreamProfile(serial, storedProfile, activeProfile) {
  return Boolean(
    storedProfile
    && storedProfile !== activeProfile
    && !isNetworkDeviceSerial(serial)
  )
}
