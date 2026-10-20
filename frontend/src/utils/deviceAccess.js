export const isAppDevice = (device) => device?.is_app_device === true || device?.connection_type === 'autojs6' ||
  (device?.platform === 'ios' && device?.connection_type === 'wda' && device?.serial?.startsWith('ios:'))

export function appStatusBadge(device) {
  const online = device?.app_presence?.status === 'ONLINE'
  return { label: online ? 'App 在线' : 'App 离线', type: online ? 'success' : 'info' }
}

export function isUsbDevice(device) {
  if (!device || isAppDevice(device)) return false
  if (device.connection_type === 'remote_usb') return true
  return !device.wireless_enabled && device.connection_type !== 'network' &&
    !(device.platform !== 'ios' && /:\d+$/.test(device.serial || ''))
}

export function connectionBadge(device) {
  if (!device) return null
  if (isAppDevice(device)) return appStatusBadge(device)
  if (isUsbDevice(device)) {
    const online = ['IDLE', 'BUSY', 'FASTBOT_RUNNING', 'WDA_DOWN'].includes(device.status)
    return { label: online ? 'USB 在线' : 'USB 离线', type: online ? 'success' : 'info' }
  }
  if (device.wireless_enabled) return { label: 'WDA 无线', type: 'success' }
  if (device.connection_type === 'network') return { label: 'WiFi 已配对', type: 'info' }
  if (device.platform !== 'ios' && /:\d+$/.test(device.serial || '')) return { label: 'ADB 网络', type: 'info' }
  return null
}
