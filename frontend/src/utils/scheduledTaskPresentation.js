const taskConfig = task => (
  task?.strategy_config && typeof task.strategy_config === 'object'
    ? task.strategy_config
    : {}
)

const findById = (items, id) => (
  id === null || id === undefined
    ? null
    : (items || []).find(item => String(item?.id) === String(id)) || null
)

const packageLabel = item => {
  if (!item) return ''
  const name = item.app_name || item.package_name || '安装包'
  const version = item.version_name || item.version_code || ''
  return `${name}${version ? ` ${version}` : ''}`
}

export const scheduledTaskType = task => {
  const type = String(taskConfig(task)._task_type || 'ui').toLowerCase()
  if (type === 'fastbot') return 'fastbot'
  if (type === 'inspection') return 'inspection'
  return 'ui'
}

export const scheduledTaskExecution = (task, lookups = {}) => {
  const config = taskConfig(task)
  const type = scheduledTaskType(task)

  if (type === 'fastbot') {
    const minutes = Math.max(1, Math.round(Number(config.fb_duration || 1800) / 60))
    const throttle = Number(config.fb_throttle)
    return {
      type,
      typeLabel: '智能探索',
      tagType: 'warning',
      title: config.fb_package_name || '未设置目标应用',
      detail: `${minutes} 分钟${Number.isFinite(throttle) ? ` · ${throttle} ms/次` : ''}`,
    }
  }

  if (type === 'inspection') {
    const profile = findById(lookups.inspectionProfiles, config.inspection_profile_id)
    const pkg = findById(lookups.packages, config.inspection_package_id)
    const selectedPackage = packageLabel(pkg) || (config.inspection_package_id ? '指定安装包' : '')
    const branches = Array.isArray(config.inspection_branches) ? config.inspection_branches : []
    const branchLabel = branches
      .map(item => ({ guest: '未登录', authenticated: '已登录' }[item] || item))
      .join('、')
    return {
      type,
      typeLabel: '智能巡检',
      tagType: 'success',
      title: profile?.name || task?.inspection_profile_name || '巡检配置',
      detail: [branchLabel || '未选择业务线', selectedPackage].filter(Boolean).join(' · '),
    }
  }

  const environment = findById(lookups.environments, config.env_id)
  return {
    type,
    typeLabel: 'UI 自动化',
    tagType: '',
    title: task?.scenario_name || '场景已删除',
    detail: environment?.name ? `环境：${environment.name}` : '',
  }
}
