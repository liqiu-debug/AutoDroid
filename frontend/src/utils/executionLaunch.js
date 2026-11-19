import { createUuid } from './uuid.js'

export const UNKNOWN_LAUNCH_MESSAGE = '启动结果待确认，请先查看执行状态；不要重新发起执行'
const unresolvedLaunches = new Map()
const inFlightLaunches = new Map()
const launchTarget = selection => `${selection.kind}:${selection.targetId}`
export const pendingExecution = (kind, targetId) => unresolvedLaunches.get(`${kind}:${targetId}`) || null
// Call only for a definitive submission reply or explicit business rejection.
export const settlePendingExecution = (kind, targetId) => unresolvedLaunches.delete(`${kind}:${targetId}`)
export function rememberWebsocketExecution(kind, targetId, devices, accepted = false) {
  const pending = { kind, targetId, devices: [...devices], transport: 'websocket', accepted, retryable: false }
  unresolvedLaunches.set(`${kind}:${targetId}`, pending)
  return pending
}
const unresolvedTombstone = launch => ({ kind: launch.kind, targetId: launch.targetId, devices: [], requestId: null, engineInstanceId: null, sessionKey: null, retryable: false })

export function isLaunchOutcomeUnknown(error) {
  return Boolean(error?.launchPending) || (!error?.response && (error?.request || ['ECONNABORTED', 'ETIMEDOUT', 'ERR_NETWORK', 'ERR_CANCELED'].includes(error?.code)))
    || error?.response?.status === 408 || error?.response?.status >= 500
}

// A retry always carries the original target and selections. Changing the form
// must never turn an uncertain submission into a second execution.
export function submitExecution(api, selection, pending = null) {
  const key = launchTarget(selection)
  if (inFlightLaunches.has(key)) return inFlightLaunches.get(key)
  const frozen = { ...selection, devices: [...selection.devices] }
  // Register synchronously before capability discovery: a navigating component
  // may submit the same target while the first component still awaits discovery.
  const promise = Promise.resolve().then(() => performSubmission(api, frozen, pending))
  inFlightLaunches.set(key, promise)
  const clear = () => { if (inFlightLaunches.get(key) === promise) inFlightLaunches.delete(key) }
  promise.then(clear, clear)
  return promise
}

async function performSubmission(api, selection, pending) {
  const key = launchTarget(selection)
  pending = pending || unresolvedLaunches.get(key) || null
  let capabilities
  try { capabilities = await api.getExecutionCapabilities({ refresh: Boolean(pending) }) } catch (error) {
    if (pending) error.launchPending = pending
    throw error
  }
  const sessionKey = api.getExecutionSessionKey()
  const canRetry = capabilities.clientMode === true && capabilities.launch_idempotency === true && Boolean(capabilities.engine_instance_id)
  const launch = pending || {
    kind: selection.kind,
    targetId: selection.targetId,
    envId: selection.envId,
    devices: [...selection.devices],
    requestId: canRetry ? createUuid() : null,
    engineInstanceId: canRetry ? capabilities.engine_instance_id : null,
    sessionKey,
    retryable: canRetry,
  }
  if (pending && (!pending.retryable || sessionKey !== pending.sessionKey || capabilities.engine_instance_id !== pending.engineInstanceId || !canRetry)) {
    const error = new Error('登录会话或桌面引擎已变化，请查看原执行记录后再运行')
    error.launchPending = unresolvedTombstone(launch)
    unresolvedLaunches.set(key, error.launchPending)
    throw error
  }
  unresolvedLaunches.set(key, launch)
  try {
    const response = await (launch.kind === 'case' ? api.runTestCaseBatch : api.runScenario)(launch.targetId, launch.envId, launch.devices, launch)
    unresolvedLaunches.delete(key)
    return { ...response, launch }
  } catch (error) {
    if (isLaunchOutcomeUnknown(error)) error.launchPending = launch
    if (error.response?.status === 409 || (pending && error.response?.status === 401)) {
      error.launchPending = unresolvedTombstone(launch)
    }
    if (error.launchPending) unresolvedLaunches.set(key, error.launchPending)
    else unresolvedLaunches.delete(key)
    throw error
  }
}
