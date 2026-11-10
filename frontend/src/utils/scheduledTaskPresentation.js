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

export const scheduledTaskType = task => {
  const type = String(taskConfig(task)._task_type || 'ui').toLowerCase()
  if (type === 'fastbot') return 'fastbot'
  if (type === 'api') return 'api'
  return 'ui'
}

export const scheduledTaskExecution = (task, lookups = {}) => {
  const config = taskConfig(task)
  const type = scheduledTaskType(task)

  if (type === 'api') {
    const environment = findById(lookups.environments, config.env_id)
    return { type, typeLabel: '接口自动化', tagType: 'primary', title: task?.scenario_name || '接口场景',
      detail: environment?.name ? `环境：${environment.name} · 无需设备` : '无需设备' }
  }

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

  const environment = findById(lookups.environments, config.env_id)
  return {
    type,
    typeLabel: 'UI 自动化',
    tagType: '',
    title: task?.scenario_name || '场景已删除',
    detail: environment?.name ? `环境：${environment.name}` : '',
  }
}
