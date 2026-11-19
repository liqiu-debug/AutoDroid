// A batch may contain both running and queued devices. Do not label queued work
// as already started; preserve its earliest known queue position.
export function describeRunSubmission(data, deviceCount) {
  const runs = Array.isArray(data?.runs) ? data.runs : []
  const queued = runs.filter(run => run.queued || String(run.status || '').toUpperCase() === 'QUEUED')
  const started = runs.length ? runs.length - queued.length : deviceCount
  const positions = queued.map(run => run.queue_position).filter(Number.isFinite)
  const queueSuffix = positions.length ? `（最前第 ${Math.min(...positions)} 位）` : ''
  const pending = runs.filter(run => run.cloud_record_pending).length
  const warning = pending ? `；${pending} 台云端执行记录尚未确认，报告可能暂不可用` : ''
  if (!queued.length) return `已在 ${started} 台设备开始执行${warning}`
  if (!started) return `已加入执行队列：${queued.length} 台${queueSuffix}${warning}`
  return `已开始 ${started} 台；${queued.length} 台排队中${queueSuffix}${warning}`
}

export function acceptedDeviceSerials(data, requested = []) {
  if (Array.isArray(data?.device_serials)) return data.device_serials.filter(Boolean)
  if (Array.isArray(data?.runs) && data.runs.length) return data.runs.map(run => run.device_serial).filter(Boolean)
  const blocked = new Set((data?.blocked_prechecks || []).map(item => item.device_serial))
  return requested.filter(serial => !blocked.has(serial))
}

export function precheckFailureReason(payload) {
  if (!payload || typeof payload !== 'object') return '预检失败'
  const failedCase = (payload.cases || []).find(item => item?.status === 'FAIL')
  if (failedCase) return `${failedCase.alias || failedCase.case_name || `用例#${failedCase.case_id}`}: ${failedCase.reason || precheckFailureReason(failedCase)}`
  const failed = [...(payload.global_checks || []), ...(payload.steps || [])].find(item => item?.status === 'FAIL')
  if (failed) return failed.message || failed.code || '步骤预检失败'
  if (payload.has_runnable_cases === false || payload.has_runnable_steps === false) return '当前设备无可执行步骤（全部跳过）'
  return '预检失败'
}

export function blockedDeviceReason(item) {
  const reason = item?.reason
  return reason && !/^(case|scenario|global|step)?\s*precheck failed/i.test(reason) ? reason : precheckFailureReason(item?.precheck)
}

export function submissionNotice(data, requested = []) {
  const devices = acceptedDeviceSerials(data, requested)
  const blocked = Array.isArray(data?.blocked_prechecks) ? data.blocked_prechecks : []
  const text = describeRunSubmission(data, devices.length)
  const suffix = blocked.length ? `；${blocked.length} 台预检失败（示例：${blocked[0].device_serial || '未知设备'} - ${blockedDeviceReason(blocked[0])}）` : ''
  return { text: text + suffix, warning: Boolean(blocked.length || data?.runs?.some(run => run.cloud_record_pending)), devices }
}
