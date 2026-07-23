const asArray = value => Array.isArray(value) ? value : []
const normalizeIssue = item => typeof item === 'string' ? { code: 'NOTICE', message: item } : (item || {})

const PAGE_ROLE_LABELS = {
  HOME: '首页',
  LIST: '列表页',
  CATALOG_CATEGORY: '分类页',
  CATEGORY: '分类页',
  PRODUCT_LIST: '商品列表',
  CONSUMABLE_LIST: '耗材列表',
  SERVICE_LIST: '服务列表',
  PRODUCT_DETAIL: '商品详情',
  SERVICE_DETAIL: '服务详情',
  PURCHASE_OPTIONS: '规格选择',
  CHECKOUT: '结算页',
  SEARCH: '搜索页',
  ORDER: '订单页',
  ORDER_DETAIL: '订单详情',
  PAYMENT: '支付页',
  CASHIER: '收银台',
  STORE_LIST: '附近门店',
  STORE_DETAIL: '门店详情',
  COMMUNITY_FEED: '许愿池',
  CART: '购物车',
  PROFILE: '我的',
  MINE: '我的',
  DIALOG: '弹窗',
  MODAL_PANEL: '弹窗',
  FILTER_DIALOG: '筛选弹窗',
  APPOINTMENT_LIST: '预约列表',
  OPAQUE: '内容页',
  UNKNOWN: '页面',
}

const STATUS_LABELS = {
  PENDING: '等待执行',
  RUNNING: '执行中',
  PASS: '通过',
  WARNING: '需关注',
  BLOCKED: '已安全拦截',
  FAIL: '失败',
  ERROR: '执行异常',
  CANCELLED: '已取消',
  ABORTED: '已中止',
}

const FAILURE_LABELS = {
  SAFETY_BLOCKED: '已到达安全边界',
  APP_FAULT: '应用发生故障',
  AUTOMATION_FAILED: '自动化执行失败',
  EXTERNAL_NAVIGATION: '进入外部页面',
  INFRA_FAULT: '设备或环境异常',
  LOCATOR_FAILED: '没有找到目标控件',
  PATH_DIVERGED: '页面与历史链路不一致',
  BUDGET_STOP: '达到本次运行上限',
  CANCELLED: '任务已取消',
  ROOT_NOT_REACHED: '无法回到链路起点',
  CHECKPOINT_DIVERGED: '页面与历史检查点不一致',
}

const isPresent = value => value !== undefined && value !== null && String(value).trim() !== ''
const positiveInteger = value => {
  const number = Number(value)
  return Number.isInteger(number) && number > 0 ? number : null
}

const stripInternalPageIds = value => String(value || '')
  .replace(/\s*\(\s*S\d+\s*\)\s*/gi, ' ')
  .replace(/(^|\s)S\d+(?=\s|$)/gi, '$1')
  .replace(/\s+/g, ' ')
  .trim()

const roleLabel = value => {
  const normalized = String(value || '').trim().toUpperCase()
  return PAGE_ROLE_LABELS[normalized] || ''
}

const readablePageName = value => {
  const cleaned = stripInternalPageIds(value)
  if (!cleaned) return ''
  return roleLabel(cleaned) || cleaned
}

const pageNameOf = item => (
  readablePageName(
    item?.page_name
      || item?.display_name
      || item?.title
      || item?.name,
  )
  || roleLabel(item?.page_subtype)
  || roleLabel(item?.role)
  || roleLabel(item?.page_role)
  || '页面'
)

const explicitPageLabel = item => {
  const label = String(
    item?.display_label
      || item?.page_label
      || item?.state_display_label
      || item?.source_display_label
      || '',
  ).trim().toUpperCase()
  if (!/^P\d+$/.test(label)) return ''
  return `P${label.slice(1).padStart(3, '0')}`
}

const observationIndexOf = item => positiveInteger(
  item?.observation_index
    ?? item?.source_observation_index
    ?? item?.capture_index
    ?? item?.observation_ordinal,
)

const stateIdOf = item => positiveInteger(
  item?.state_id
    ?? item?.source_state_id
    ?? item?.endpoint_state_id,
)

const observationIdOf = item => positiveInteger(
  item?.source_observation_id
    ?? item?.observation_id,
)

const createFrozenReferenceContext = chains => {
  const records = []
  asArray(chains).forEach(chain => {
    const checkpoints = asArray(chain?.checkpoints)
    records.push(...checkpoints)
    records.push({
      ...chain,
      state_id: chain?.endpoint_state_id ?? chain?.source_state_id,
      display_label: chain?.source_display_label || chain?.display_label,
      observation_index: chain?.source_observation_index ?? chain?.observation_index,
    })
  })

  const stateOrder = []
  const labelsByState = new Map()
  records.forEach(item => {
    const stateId = stateIdOf(item)
    if (!stateId) return
    if (!stateOrder.includes(stateId)) stateOrder.push(stateId)
    const label = explicitPageLabel(item)
    if (label) labelsByState.set(stateId, label)
  })
  const width = Math.max(3, String(Math.max(1, stateOrder.length)).length)
  stateOrder.forEach((stateId, index) => {
    if (!labelsByState.has(stateId)) {
      labelsByState.set(stateId, `P${String(index + 1).padStart(width, '0')}`)
    }
  })

  const observationIndexes = new Map()
  const nextObservationIndex = new Map()
  records.forEach(item => {
    const stateId = stateIdOf(item)
    const observationId = observationIdOf(item)
    if (!stateId || !observationId) return
    const key = `${stateId}:${observationId}`
    const explicit = observationIndexOf(item)
    if (explicit) {
      observationIndexes.set(key, explicit)
      nextObservationIndex.set(stateId, Math.max(nextObservationIndex.get(stateId) || 1, explicit + 1))
      return
    }
    if (!observationIndexes.has(key)) {
      const next = nextObservationIndex.get(stateId) || 1
      observationIndexes.set(key, next)
      nextObservationIndex.set(stateId, next + 1)
    }
  })

  return { labelsByState, observationIndexes }
}

const normalizeCheckpoint = (item, index, context) => {
  const checkpoint = item || {}
  const stateId = stateIdOf(checkpoint)
  const observationId = observationIdOf(checkpoint)
  const displayLabel = explicitPageLabel(checkpoint) || context.labelsByState.get(stateId) || ''
  const observationIndex = observationIndexOf(checkpoint)
    || context.observationIndexes.get(`${stateId}:${observationId}`)
    || (observationId ? 1 : null)
  const pageName = pageNameOf(checkpoint)
  return {
    ...checkpoint,
    checkpoint_index: Number(checkpoint.checkpoint_index ?? index),
    display_label: displayLabel,
    page_name: pageName,
    observation_index: observationIndex,
    display_reference: [displayLabel, pageName].filter(Boolean).join(' · '),
    capture_label: observationIndex ? `第 ${observationIndex} 次采集` : '代表采集',
  }
}

const sourceReferenceFor = (chain, checkpoints, context) => {
  const endpoint = checkpoints.at(-1) || {}
  const stateId = positiveInteger(chain?.source_state_id ?? chain?.endpoint_state_id) || stateIdOf(endpoint)
  const observationId = observationIdOf(chain) || observationIdOf(endpoint)
  const displayLabel = explicitPageLabel(chain) || endpoint.display_label || context.labelsByState.get(stateId) || ''
  const pageName = pageNameOf({
    ...endpoint,
    page_name: chain?.source_page_name || chain?.endpoint_page_name || endpoint.page_name || chain?.name,
  })
  const observationIndex = observationIndexOf(chain)
    || endpoint.observation_index
    || context.observationIndexes.get(`${stateId}:${observationId}`)
    || (observationId ? 1 : null)
  return {
    source_display_label: displayLabel,
    source_page_name: pageName,
    source_observation_index: observationIndex,
    source_reference: [displayLabel, pageName].filter(Boolean).join(' · ') || pageName,
    source_capture_label: observationIndex ? `第 ${observationIndex} 次采集` : '代表采集',
  }
}

const normalizeRouteId = value => {
  if (!isPresent(value)) return { present: false, id: null }
  const id = positiveInteger(value)
  return id
    ? { present: true, id }
    : { present: true, id: null, error: '链接中的巡检报告编号无效' }
}

export const resolveInspectionRunRouteSelection = ({
  routeValue,
  recentRuns = [],
  routeRun = null,
  loadError = '',
} = {}) => {
  const parsed = normalizeRouteId(routeValue)
  const runs = asArray(recentRuns)
  if (!parsed.present) {
    return {
      explicit: false,
      selectionId: runs[0]?.id || '',
      options: runs,
      blocker: '',
    }
  }
  if (!parsed.id) {
    return { explicit: true, selectionId: '', options: runs, blocker: parsed.error }
  }
  if (loadError || Number(routeRun?.id) !== parsed.id) {
    return {
      explicit: true,
      selectionId: '',
      options: runs,
      blocker: loadError || `无法加载巡检报告 #${parsed.id}，已停止创建回放任务`,
    }
  }
  return {
    explicit: true,
    selectionId: parsed.id,
    options: [routeRun, ...runs.filter(item => Number(item?.id) !== parsed.id)],
    blocker: '',
  }
}

const parseJsonValue = value => {
  if (typeof value !== 'string') return value
  try {
    return JSON.parse(value)
  } catch {
    return value
  }
}

export const normalizeReplayScope = value => ({
  FULL: 'FULL_PATH',
  FULL_PATH: 'FULL_PATH',
  SAFE_PREFIX: 'PREFIX_TO_SAFETY_BOUNDARY',
  PREFIX_TO_SAFETY_BOUNDARY: 'PREFIX_TO_SAFETY_BOUNDARY',
  DIAGNOSTIC_ONLY: 'DIAGNOSTIC_ONLY',
  NONE: 'NONE',
}[String(value || 'NONE').trim().toUpperCase()] || 'NONE')

export const replayScopeLabel = value => ({
  FULL_PATH: '可完整回放',
  PREFIX_TO_SAFETY_BOUNDARY: '回放到安全边界',
  DIAGNOSTIC_ONLY: '仅用于问题复现',
  NONE: '不可回放',
}[normalizeReplayScope(value)] || '不可回放')

export const terminalOutcomeLabel = value => ({
  NONE: '正常结束',
  SAFETY_BLOCKED: '安全拦截',
  APP_FAULT: '应用故障',
  AUTOMATION_FAILED: '自动化执行失败',
  EXTERNAL_NAVIGATION: '进入外部页面',
  INFRA_FAULT: '设备或环境故障',
  LOCATOR_FAILED: '控件定位失败',
  BUDGET_STOP: '达到任务上限',
  CANCELLED: '任务取消',
}[String(value || 'NONE').trim().toUpperCase()] || value || '正常结束')

export const boundaryEvidenceLabel = value => ({
  VERIFIED: '边界已确认',
  NOT_VERIFIABLE: '边界无法可靠确认',
  CHANGED: '边界已变化',
  NOT_APPLICABLE: '无需边界确认',
  NOT_RUN: '尚未执行边界检查',
  UNKNOWN: '边界待确认',
}[String(value || 'NOT_APPLICABLE').trim().toUpperCase()] || value || '无需边界确认')

export const sourceBoundaryEvidenceLabel = value => ({
  VERIFIED: '源报告已确认',
  NOT_VERIFIABLE: '源报告无法确认',
  CHANGED: '源报告记录边界变化',
  NOT_APPLICABLE: '源报告无安全边界',
  UNKNOWN: '源报告边界证据不足',
}[String(value || 'NOT_APPLICABLE').trim().toUpperCase()] || '源报告边界证据不足')

export const replayBoundaryEvidenceLabel = value => ({
  VERIFIED: '升级后边界仍一致',
  NOT_VERIFIABLE: '升级后无法确认边界',
  CHANGED: '升级后边界已变化',
  NOT_APPLICABLE: '本次无需检查边界',
  NOT_RUN: '升级后尚未检查边界',
  UNKNOWN: '升级后边界结果未知',
}[String(value || 'NOT_RUN').trim().toUpperCase()] || '升级后边界结果未知')

export const compatibilityStatusLabel = value => (
  STATUS_LABELS[String(value || 'PENDING').trim().toUpperCase()] || '状态未知'
)

export const replayFailureLabel = value => {
  const normalized = String(value || '').trim().toUpperCase()
  if (!normalized || normalized === 'NONE') return ''
  return FAILURE_LABELS[normalized] || (normalized ? '执行未完成' : '')
}

export const replayRoleLabel = value => roleLabel(value) || readablePageName(value) || '页面'

export const replayActionLabel = value => {
  const normalized = String(value || '').trim().toUpperCase()
  if (!normalized) return '检查页面'
  if (normalized.startsWith('ITEM_OPEN')) return '打开内容'
  if (normalized.startsWith('CATEGORY_TAB')) return '切换分类'
  if (normalized.startsWith('SORT')) return '调整排序'
  if (normalized.startsWith('FILTER')) return '打开筛选'
  if (normalized.startsWith('SCROLL')) return '浏览更多内容'
  if (normalized.startsWith('NAV')) return '切换页面'
  if (normalized.startsWith('INPUT')) return '填写内容'
  if (normalized.startsWith('TOGGLE')) return '切换选项'
  if (normalized.startsWith('COMMAND')) return '执行操作'
  if (normalized === 'ROOT' || normalized === 'ROOT_CHECK') return '确认链路起点'
  if (normalized.includes('BOUNDARY')) return '确认安全边界'
  return readablePageName(value) || '执行操作'
}

export const replayCheckpointLabel = checkpoint => (
  checkpoint?.display_reference
    || [explicitPageLabel(checkpoint), pageNameOf(checkpoint)].filter(Boolean).join(' · ')
)

export const replayPathLabel = chain => {
  const checkpoints = asArray(chain?.checkpoints)
  if (checkpoints.length) return checkpoints.map(replayCheckpointLabel).filter(Boolean).join(' → ')
  return asArray(chain?.covered_roles).map(replayRoleLabel).join(' → ') || '单页面检查'
}

const terminalOutcomeOf = chain => {
  const explicit = String(chain?.terminal_outcome || '').trim().toUpperCase()
  if (explicit) return explicit
  const outcomes = new Set(asArray(chain?.terminal_boundaries).map(item => (
    String(item?.terminal_outcome || 'NONE').trim().toUpperCase()
  )))
  return [
    'APP_FAULT', 'INFRA_FAULT', 'AUTOMATION_FAILED', 'EXTERNAL_NAVIGATION',
    'LOCATOR_FAILED', 'SAFETY_BLOCKED', 'BUDGET_STOP', 'CANCELLED',
  ].find(item => outcomes.has(item)) || 'NONE'
}

export const compatibilityExecutionMode = run => (
  String(run?.execution_mode || '').toLowerCase() === 'installed_replay'
    ? 'installed_replay'
    : 'comparison'
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

export const normalizeReplayPreflight = payload => {
  const data = payload || {}
  const plan = data.plan || {}
  const rawChains = asArray(data.chains).length ? asArray(data.chains) : asArray(plan.chains)
  const rawBlockers = asArray(data.blockers).length ? asArray(data.blockers) : asArray(plan.blockers)
  const warnings = asArray(data.warnings).length ? asArray(data.warnings) : asArray(plan.warnings)
  // A same-version or unknown-source run is still useful for reachability.
  // Keep those notices visible, but do not make the user wait for an APK
  // comparison that this lightweight mode intentionally does not perform.
  const softBlocker = item => {
    const code = String(item?.code || item?.type || '').toUpperCase()
    const message = String(item?.message || '').toLowerCase()
    return [
      'SAME_VERSION',
      'SAME_VERSION_REPLAY',
      'VERSION_NOT_NEWER',
      'SOURCE_VERSION_UNKNOWN',
      'SOURCE_PACKAGE_UNKNOWN',
      'VERSION_UNKNOWN',
      'TARGET_VERSION_UNKNOWN',
    ].includes(code) || message.includes('same version') || message.includes('版本相同') || message.includes('版本未知')
  }
  const blockers = rawBlockers.filter(item => !softBlocker(item)).map(normalizeIssue)
  const softWarnings = rawBlockers.filter(softBlocker).map(normalizeIssue)
  const installedPackage = data.installed_package || data.target_package || data.device?.installed_package
    || data.devices?.[0]?.installed_package || {}
  const sourcePackage = data.source_package || data.source || {}
  const referenceContext = createFrozenReferenceContext(rawChains)

  return {
    ...data,
    branch_key: data.branch_key || data.replay_branch_key || '',
    plan_digest: data.plan_digest || plan.plan_digest || '',
    plan_version: data.plan_version || plan.plan_version || '',
    source_package: sourcePackage,
    installed_package: installedPackage,
    blockers,
    warnings: [...warnings.map(normalizeIssue), ...softWarnings],
    summary: data.summary || plan.summary || {},
    excluded: data.excluded || plan.excluded || {},
    chains: rawChains.map((chain, index) => {
      const replayScope = normalizeReplayScope(chain.replay_scope || chain.replay_eligibility)
      const checkpoints = asArray(chain.checkpoints).map((item, checkpointIndex) => (
        normalizeCheckpoint(item, checkpointIndex, referenceContext)
      ))
      const sourceReference = sourceReferenceFor(chain, checkpoints, referenceContext)
      return {
        ...chain,
        chain_id: String(chain.chain_id || chain.path_key || chain.id || `chain-${index + 1}`),
        path_key: chain.path_key || chain.chain_id || chain.id || '',
        name: readablePageName(chain.name || chain.title || chain.page_name) || sourceReference.source_page_name || `链路 ${index + 1}`,
        evidence_level: chain.evidence_level || chain.evidence_grade || 'OBSERVED_ONCE',
        replay_scope: replayScope,
        replay_eligibility: replayScope === 'FULL_PATH'
          ? 'FULL'
          : replayScope === 'PREFIX_TO_SAFETY_BOUNDARY' ? 'SAFE_PREFIX' : replayScope,
        terminal_outcome: terminalOutcomeOf(chain),
        boundary_evidence: chain.boundary_evidence || 'NOT_APPLICABLE',
        source_boundary_evidence: chain.source_boundary_evidence || chain.boundary_evidence || 'NOT_APPLICABLE',
        replay_boundary_evidence: 'NOT_RUN',
        checkpoints,
        covered_roles: asArray(chain.covered_roles),
        depth: Number(chain.depth ?? asArray(chain.checkpoints).length ?? 0),
        ...sourceReference,
      }
    }),
  }
}

const candidateAssets = result => ({
  candidate_screenshot_asset_id: result.candidate_screenshot_asset_id || result.screenshot_asset_id || '',
  candidate_screenshot_path: result.candidate_screenshot_path || result.screenshot_path || '',
  candidate_xml_asset_id: result.candidate_xml_asset_id || result.xml_asset_id || '',
  candidate_xml_path: result.candidate_xml_path || result.xml_path || '',
})

export const normalizeReplayResults = run => {
  const snapshotChains = asArray(run?.page_set_snapshot)
  const referenceContext = createFrozenReferenceContext(snapshotChains)
  // The persisted result intentionally keeps both the stable chain id and the
  // path digest. Older rows use the digest as `path_key`, while newer rows use
  // the prefixed `chain-*` id as `page_key`. Index both aliases so report
  // metadata (checkpoints, covered roles, evidence) survives either format.
  const chainByKey = new Map()
  snapshotChains.forEach(chain => {
    const aliases = [chain.chain_id, chain.path_key, chain.id]
    aliases.forEach(key => {
      const normalized = String(key || '')
      if (normalized) chainByKey.set(normalized, chain)
    })
  })
  const direct = asArray(run?.page_results).length
    ? asArray(run.page_results)
    : asArray(run?.replay_results)
  const rows = direct.length
    ? direct.map(result => ({ result, cell: null }))
    : asArray(run?.cells).flatMap(cell => asArray(cell.pages).map(result => ({ result, cell })))

  return rows.map(({ result, cell }, index) => {
    const resultAliases = [result.chain_id, result.page_key, result.path_key]
      .map(value => String(value || ''))
      .filter(Boolean)
    const chain = resultAliases.map(key => chainByKey.get(key)).find(Boolean) || {}
    const chainId = String(chain.chain_id || result.chain_id || result.page_key || result.path_key || result.id || index + 1)
    const rawCheckpoints = asArray(result.checkpoints).length ? asArray(result.checkpoints) : asArray(chain.checkpoints)
    const checkpoints = rawCheckpoints.map((item, checkpointIndex) => (
      normalizeCheckpoint(item, checkpointIndex, referenceContext)
    ))
    const sourceReference = sourceReferenceFor({ ...chain, ...result }, checkpoints, referenceContext)
    const replayTrace = normalizeReplayTrace(result.replay_trace)
    const replayScope = normalizeReplayScope(
      result.replay_scope
        || result.metrics?.replay_scope
        || chain.replay_scope
        || result.replay_eligibility
        || result.metrics?.replay_eligibility
        || chain.replay_eligibility,
    )
    const terminalOutcome = terminalOutcomeOf({
      ...chain,
      ...result,
      terminal_outcome: result.terminal_outcome || result.metrics?.terminal_outcome,
      terminal_boundaries: result.terminal_boundaries || result.metrics?.terminal_boundaries || chain.terminal_boundaries,
    })
    const expectsBoundaryCheck = replayScope === 'PREFIX_TO_SAFETY_BOUNDARY'
      || terminalOutcome === 'SAFETY_BLOCKED'
    const traceBoundaryEvidence = [...replayTrace]
      .reverse()
      .map(item => item.boundary_evidence)
      .find(isPresent)
    const sourceBoundaryEvidence = result.source_boundary_evidence
      || result.metrics?.source_boundary_evidence
      || chain.source_boundary_evidence
      || chain.boundary_evidence
      || 'NOT_APPLICABLE'
    const replayBoundaryEvidence = result.replay_boundary_evidence
      || result.execution_boundary_evidence
      || result.metrics?.replay_boundary_evidence
      || result.metrics?.execution_boundary_evidence
      || traceBoundaryEvidence
      || (['PENDING', 'RUNNING'].includes(String(result.status || '').toUpperCase())
        ? 'NOT_RUN'
        : expectsBoundaryCheck ? 'UNKNOWN' : 'NOT_APPLICABLE')
    return {
      ...result,
      ...candidateAssets(result),
      id: result.id || result.page_result_id || `${cell?.id || 'result'}-${index}`,
      chain_id: chainId,
      path_key: result.path_key || result.page_key || result.chain_id || chain.path_key || '',
      source_state_id: result.source_state_id || chain.endpoint_state_id || chain.source_state_id || null,
      source_observation_id: result.source_observation_id || chain.source_observation_id || null,
      name: readablePageName(result.name || result.page_name || result.title || chain.name) || sourceReference.source_page_name || `链路 ${index + 1}`,
      checkpoints,
      covered_roles: asArray(result.covered_roles).length ? asArray(result.covered_roles) : asArray(chain.covered_roles),
      covered_subtypes: asArray(result.covered_subtypes).length ? asArray(result.covered_subtypes) : asArray(chain.covered_subtypes),
      duration_ms: result.duration_ms ?? result.metrics?.duration_ms ?? null,
      checkpoint_count: result.checkpoint_count ?? result.metrics?.checkpoint_count ?? asArray(chain.checkpoints).length,
      device_serial: result.device_serial || cell?.device_serial || run?.device_serials?.[0] || '',
      evidence_level: result.evidence_level || result.evidence_grade || chain.evidence_level || '-',
      replay_scope: replayScope,
      terminal_outcome: terminalOutcome,
      boundary_evidence: sourceBoundaryEvidence,
      source_boundary_evidence: sourceBoundaryEvidence,
      replay_boundary_evidence: replayBoundaryEvidence,
      failure_type: result.failure_type || result.failure_code || '',
      failed_step_index: result.failed_step_index ?? result.failure_step_index ?? null,
      replay_trace: replayTrace,
      ...sourceReference,
    }
  })
}

export const normalizeReplayTrace = trace => {
  const parsed = parseJsonValue(trace)
  const steps = Array.isArray(parsed) ? parsed : asArray(parsed?.steps)
  return steps.map((step, index) => {
    const expectedRole = step.expected_page_subtype
      || step.expected_page_role
      || step.expected_role
      || step.source?.actual?.page_subtype
      || step.source?.expected?.page_subtype
      || step.source?.actual?.role
      || step.source?.expected?.role
      || ''
    const actualRole = step.actual_page_subtype
      || step.actual_page_role
      || step.actual_role
      || step.target?.actual?.page_subtype
      || step.target?.expected?.page_subtype
      || step.target?.actual?.role
      || step.target?.expected?.role
      || ''
    return {
      ...step,
      index: Number(step.step_index ?? step.index ?? index),
      name: replayActionLabel(step.name || step.action_role || step.action_role_key || step.action),
      raw_name: step.name || step.action_role || step.action_role_key || step.action || '',
      status: String(step.status || step.result || 'UNKNOWN').toUpperCase(),
      status_label: compatibilityStatusLabel(step.status || step.result || 'UNKNOWN'),
      duration_ms: step.duration_ms ?? step.elapsed_ms ?? null,
      expected_role: expectedRole,
      actual_role: actualRole,
      expected_role_label: expectedRole ? replayRoleLabel(expectedRole) : '未记录',
      actual_role_label: actualRole ? replayRoleLabel(actualRole) : '未记录',
      expected_anchor: step.expected_instance_anchor || step.expected_anchor || step.source?.expected?.instance_anchor || '',
      actual_anchor: step.actual_instance_anchor || step.actual_anchor || step.target?.actual?.instance_anchor || '',
      reason: step.reason || step.message || step.failure_reason || '',
      boundary_evidence: step.replay_boundary_evidence || step.execution_boundary_evidence || step.boundary_evidence || '',
    }
  })
}
