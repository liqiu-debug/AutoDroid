const STATUS_LABELS = {
  PENDING: '等待执行',
  RUNNING: '执行中',
  PASS: '通过',
  WARNING: '需关注',
  FAIL: '失败',
  ERROR: '执行异常',
  CANCELLED: '已取消',
  ABORTED: '已中止',
}

export const compatibilityStatusLabel = value => (
  STATUS_LABELS[String(value || 'PENDING').trim().toUpperCase()] || '状态未知'
)

export const packageSnapshotLabel = snapshot => {
  const value = snapshot || {}
  const name = value.app_name || value.package_name || ''
  const versionName = value.version_name || value.versionName || ''
  const versionCode = value.version_code ?? value.versionCode
  const version = [versionName, versionCode !== undefined && versionCode !== null && versionCode !== '' ? `(${versionCode})` : '']
    .filter(Boolean)
    .join(' ')
  return [name, version].filter(Boolean).join(' ') || '版本未知'
}
