export const INSPECTION_ACTION_STATUS_META = Object.freeze({
  PENDING: { label: '待执行', type: 'success' },
  QUEUED: { label: '待执行', type: 'success' },
  READY: { label: '待执行', type: 'success' },
  RUNNING: { label: '执行中', type: 'warning' },
  STARTED: { label: '执行中', type: 'warning' },
  ACTION_STARTED: { label: '执行中', type: 'warning' },
  INVOKING: { label: '执行中', type: 'warning' },
  ACTIVE: { label: '执行中', type: 'warning' },
  INVOKED: { label: '已调用', type: 'info' },
  PASS: { label: '通过', type: 'success' },
  SELF_LOOP: { label: '同页', type: 'info' },
  NO_EFFECT: { label: '无响应', type: 'info' },
  BLOCKED: { label: '已拦截', type: 'danger' },
  COORDINATE_ONLY: { label: '仅坐标', type: 'warning' },
  COORDINATE_UNSAFE: { label: '坐标不安全', type: 'warning' },
  COORDINATE_STALE: { label: '坐标已过期', type: 'warning' },
  AMBIGUOUS: { label: '定位歧义', type: 'warning' },
  LOCATOR_AMBIGUOUS: { label: '定位歧义', type: 'warning' },
  LOCATOR_NOT_FOUND: { label: '定位器缺失', type: 'warning' },
  LOCATOR_DRIFT: { label: '定位漂移（历史）', type: 'warning' },
  PARENT_RECOVERY_FAILED: { label: '父页恢复失败', type: 'warning' },
  PARENT_RECOVERY_CASCADE: { label: '父页恢复失败（批次）', type: 'warning' },
  PATH_DIVERGED: { label: '路径偏离', type: 'warning' },
  ACTION_ERROR: { label: '动作异常', type: 'danger' },
  ERROR: { label: '异常', type: 'danger' },
  SKIPPED: { label: '已跳过', type: 'info' },
  UNSTABLE_PARENT: { label: '父状态不稳定（历史）', type: 'info' },
  NOT_REACHED: { label: '收尾未执行（历史）', type: 'info' },
  CANCELLED: { label: '任务取消', type: 'info' },
  BUDGET_LIMIT: { label: '达到预算上限', type: 'info' },
  BUDGET_NOT_REACHED: { label: '预算未执行', type: 'info' },
  COVERED_BY_FAMILY: { label: '同构族已覆盖', type: 'info' },
  COVERAGE_EXHAUSTED: { label: '同构族覆盖上限', type: 'info' },
  FILTERED_NON_ACTIONABLE: { label: '非可执行动作', type: 'info' },
  QUEUE_TRUNCATED: { label: '队列截断', type: 'info' },
  CYCLE_CONVERGED: { label: '循环已收敛', type: 'info' },
  COVERED_BY_CONTRACT: { label: '覆盖契约复用', type: 'info' },
  SAMPLED_OUT: { label: '代表采样跳过', type: 'info' },
  NAVIGATION_REUSED: { label: '导航已复用', type: 'info' },
  VISUAL_STALE: { label: '视觉入口已变化', type: 'warning' },
  NO_NEW_COVERAGE: { label: '无新增覆盖', type: 'info' },
  OUT_OF_SCOPE: { label: '超出单页范围', type: 'info' },
})

export const NON_NUMBERED_INSPECTION_ACTION_STATUSES = new Set([
  'BLOCKED', 'AMBIGUOUS', 'LOCATOR_AMBIGUOUS', 'LOCATOR_NOT_FOUND', 'LOCATOR_DRIFT',
  'COORDINATE_ONLY', 'COORDINATE_UNSAFE', 'COORDINATE_STALE', 'PARENT_RECOVERY_FAILED', 'PARENT_RECOVERY_CASCADE',
  'PATH_DIVERGED', 'SKIPPED', 'UNSTABLE_PARENT', 'NOT_REACHED', 'CANCELLED',
  'BUDGET_LIMIT', 'BUDGET_NOT_REACHED', 'COVERED_BY_FAMILY', 'FILTERED_NON_ACTIONABLE',
  'QUEUE_TRUNCATED', 'CYCLE_CONVERGED', 'COVERAGE_EXHAUSTED',
  'COVERED_BY_CONTRACT', 'SAMPLED_OUT', 'NAVIGATION_REUSED', 'VISUAL_STALE',
  'NO_NEW_COVERAGE', 'OUT_OF_SCOPE',
])

export const INSPECTION_LOCATOR_FAILURE_STATUSES = new Set([
  'AMBIGUOUS', 'LOCATOR_AMBIGUOUS', 'LOCATOR_NOT_FOUND', 'LOCATOR_DRIFT',
  'COORDINATE_ONLY', 'COORDINATE_UNSAFE', 'COORDINATE_STALE',
  'PARENT_RECOVERY_FAILED', 'PARENT_RECOVERY_CASCADE', 'PATH_DIVERGED',
  'VISUAL_STALE',
])

export const inspectionExecutionDispositionLabel = value => ({
  EXECUTED: '已执行',
  FAMILY_REUSED: '同构族复用',
  CONTRACT_REUSED: '覆盖契约复用',
  NAVIGATION_REUSED: '导航复用',
  SAMPLED_OUT: '代表采样跳过',
  SKIPPED: '已跳过',
  FAILED: '执行失败',
  NOT_REACHED: '收尾未执行',
  RESULT_UNKNOWN: '结果未知',
  PENDING: '待执行',
}[String(value || '').toUpperCase()] || value || '-')

export const inspectionPageRoleLabel = value => ({
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
  CHECKOUT_CONFIRMATION: '结算确认',
  SEARCH: '搜索页',
  ORDER: '订单页',
  ORDER_DETAIL: '订单详情',
  PAYMENT: '支付页',
  CASHIER: '收银台',
  CART: '购物车',
  PROFILE: '我的',
  MINE: '我的',
  DIALOG: '弹窗',
  MODAL_PANEL: '弹窗',
  FILTER_DIALOG: '筛选弹窗',
  STORE_LIST: '附近门店',
  STORE_DETAIL: '门店详情',
  APPOINTMENT_LIST: '预约列表',
  COMMUNITY_FEED: '许愿池',
  COMMUNITY_DETAIL: '许愿池内容',
  AUTH_GATE: '登录门槛',
  MEMBER_BENEFITS: '会员权益',
  FAVORITES: '商品收藏',
  BROWSING_HISTORY: '历史浏览',
  OPAQUE: '不透明页面',
  UNKNOWN: '普通页面',
}[String(value || '').trim().toUpperCase()] || value || '普通页面')

export const inspectionReachabilityLabel = value => ({
  VERIFIED_TWICE: '已复验',
  REVERIFIED_ONCE: '核心终点已复验一次',
  OBSERVED_ONCE: '已到达，待复验',
  UNSTABLE: '路径不稳定',
  UNKNOWN: '尚无足够证据',
}[String(value || '').trim().toUpperCase()] || value || '尚无足够证据')

export const inspectionReplayEligibilityLabel = value => ({
  FULL: '可完整回放',
  FULL_PATH: '可完整回放',
  SAFE_PREFIX: '可安全回放前缀',
  PREFIX_TO_SAFETY_BOUNDARY: '可安全回放前缀',
  DIAGNOSTIC_ONLY: '仅用于问题复现',
  NONE: '暂不可回放',
}[String(value || '').trim().toUpperCase()] || value || '暂不可回放')

export const normalizeInspectionReplayScope = value => ({
  FULL_PATH: 'FULL',
  PREFIX_TO_SAFETY_BOUNDARY: 'SAFE_PREFIX',
  DIAGNOSTIC_ONLY: 'NONE',
  FULL: 'FULL',
  SAFE_PREFIX: 'SAFE_PREFIX',
  NONE: 'NONE',
}[String(value || '').trim().toUpperCase()] || '')

const stateObserved = state => Boolean(
  Number(state?.observation_count ?? state?.visit_count ?? 0) > 0
  || state?.screenshot_asset_id
  || state?.screenshot_path
  || state?.xml_asset_id
  || state?.xml_path,
)

export const inspectionReachabilityEvidence = state => {
  const stable = String(state?.stable_status || '').trim().toUpperCase()
  // Business coverage deliberately uses one independent endpoint replay,
  // while compatibility regression still requires two stable replays.
  if (stable === 'REVERIFIED_ONCE' && stateObserved(state)) return 'REVERIFIED_ONCE'
  const explicit = String(state?.reachability_evidence || '').trim().toUpperCase()
  if (explicit) return explicit
  if (stable === 'VERIFIED_TWICE' && stateObserved(state)) return 'VERIFIED_TWICE'
  if (['UNSTABLE', 'PATH_DIVERGED'].includes(stable)) return 'UNSTABLE'
  if (stateObserved(state)) return 'OBSERVED_ONCE'
  return 'UNKNOWN'
}

export const inspectionReplayEligibility = (state, outgoingLinks = []) => {
  const explicit = normalizeInspectionReplayScope(state?.replay_scope || state?.replay_eligibility)
  if (explicit) return explicit
  const locator = String(state?.locator_quality || '').trim().toUpperCase()
  if (['COORDINATE_ONLY', 'COORDINATE_UNSAFE', 'LOCATOR_NOT_FOUND'].includes(locator)) return 'NONE'
  const hasBoundary = outgoingLinks.some(link => (
    String(link?.terminal_outcome || '').toUpperCase() === 'SAFETY_BLOCKED'
    || ['BLOCKED', 'SAFETY_BLOCKED'].includes(String(link?.status || link?.failure_type || '').toUpperCase())
  ))
  if (hasBoundary && stateObserved(state)) return 'SAFE_PREFIX'
  if (['STABLE', 'VERIFIED', 'VERIFIED_TWICE'].includes(String(state?.stable_status || '').toUpperCase())) return 'FULL'
  return 'NONE'
}

export const inspectionTerminalOutcomeLabel = value => ({
  NONE: '无终止边界',
  SAFETY_BLOCKED: '安全拦截',
  LOCATOR_FAILED: '定位失败',
  APP_FAULT: '应用故障',
  AUTOMATION_FAILED: '自动化执行失败',
  INFRA_FAULT: '设备/基础设施故障',
  BUDGET_STOP: '达到预算',
  CANCELLED: '任务取消',
}[String(value || '').trim().toUpperCase()] || value || '无终止边界')

export const inspectionBoundaryEvidenceLabel = value => ({
  VERIFIED: '边界已核验',
  NOT_VERIFIABLE: '边界暂时无法核验',
  CHANGED: '边界已发生变化',
}[String(value || '').trim().toUpperCase()] || '')

export const inspectionCaptureKindLabel = value => ({
  DISCOVERY: '首次到达',
  REVISIT: '再次到达',
  VERIFICATION: '路径复验',
  CYCLE: '循环证据',
  FAULT: '故障证据',
  BASELINE: '回归基线',
  LEGACY: '历史采集',
  VIEWPORT: '同页视口',
}[String(value || '').trim().toUpperCase()] || '页面采集')

export const inspectionAssetAvailabilityLabel = evidence => {
  const status = String(evidence?.asset_status || '').trim().toUpperCase()
  if (status === 'CLEANED') return '完整资产已清理'
  if (status === 'METADATA_ONLY' || evidence?.metadata_only) return '仅保留采集信息'
  if (status === 'UNAVAILABLE') return '暂无可查看资产'
  const screenshot = Boolean(
    evidence?.screenshot_asset_id
      || evidence?.representative_screenshot_asset_id
      || evidence?.screenshot_path,
  )
  const xml = Boolean(
    evidence?.xml_asset_id
      || evidence?.representative_xml_asset_id
      || evidence?.xml_path,
  )
  if (screenshot && xml) return '截图和页面结构可查看'
  if (screenshot) return '截图可查看'
  if (xml) return '页面结构可查看'
  return status === 'AVAILABLE' ? '资产可查看' : '暂无可查看资产'
}

export const inspectionObservationOrdinal = ({ total, page, pageSize, index } = {}) => (
  Math.max(1, Number(total || 0) - (
    (Math.max(1, Number(page || 1)) - 1) * Number(pageSize || 0)
      + Math.max(0, Number(index || 0))
  ))
)

export const inspectionPageDisplayName = (page, fallbackLabel = '') => {
  const displayLabelMatch = [
    page?.display_label,
    page?.page_label,
    Number(page?.display_index) > 0 ? `P${Number(page.display_index)}` : '',
    fallbackLabel,
  ].map(value => String(value || '').trim().match(/^P(\d+)\b/i))
    .find(Boolean)
  const displayLabel = displayLabelMatch
    ? `P${displayLabelMatch[1].padStart(3, '0')}`
    : ''
  const role = page?.page_role || page?.template_role
  const fallbackTitle = String(fallbackLabel || '').replace(/^P\d+\s*·\s*/i, '').trim()
  const title = String(
    page?.page_title
      || page?.title
      || page?.display_title
      || (role ? inspectionPageRoleLabel(role) : '')
      || fallbackTitle,
  ).trim()
  if (displayLabel && title) {
    return `${displayLabel} · ${title}`
  }
  return displayLabel || title || '当前页面'
}

export const inspectionTerminalReviewState = (nodes = [], context = {}) => {
  if (!Array.isArray(nodes) || !nodes.length) return null
  const stateId = value => Number(value?.state_id ?? value?.id)
  const preferredStateIds = [
    context?.liveStateId,
    context?.last_active_state_id,
    context?.current_state_id,
    context?.last_state_id,
  ].map(Number).filter(Number.isFinite)
  for (const preferredId of preferredStateIds) {
    const match = nodes.find(item => stateId(item) === preferredId)
    if (match) return match
  }
  const lastObservationId = Number(context?.last_observation_id ?? context?.last_observed_observation_id)
  if (Number.isFinite(lastObservationId)) {
    const match = nodes.find(item => [
      item?.representative_observation_id,
      item?.last_observation_id,
      item?.observation_id,
    ].some(value => Number(value) === lastObservationId))
    if (match) return match
  }
  const observed = nodes
    .map((item, index) => ({ item, index, time: Date.parse(item?.last_observed_at || item?.updated_at || '') }))
    .filter(entry => Number.isFinite(entry.time))
    .sort((left, right) => right.time - left.time || right.index - left.index)
  return observed[0]?.item || nodes[nodes.length - 1] || null
}

export const inspectionReportSummary = ({ graph = {}, run = {}, nodes = [] } = {}) => {
  const source = graph?.summary || {}
  const stats = graph?.stats || {}
  const summaryAvailable = graph?.summary_available !== false
    && source?.summary_available !== false
  const familySource = source.exploration_coverage || source.page_family_coverage || source.family_coverage || {}
  const familyTotal = Number(
    familySource.total ?? familySource.discovered ?? stats.families_discovered ?? stats.families ?? run.total_families ?? 0,
  ) || 0
  const familyExpanded = Number(
    familySource.expanded ?? familySource.representatives_expanded ?? stats.family_representatives_expanded ?? 0,
  ) || 0
  const familyRatio = familyTotal > 0
    ? Number(familySource.ratio ?? familySource.coverage_ratio ?? stats.family_coverage_ratio ?? familyExpanded / familyTotal)
    : 0
  const explicitReached = source.reached_pages ?? source.pages_reached ?? stats.reached_pages
  const reached = explicitReached === null || explicitReached === undefined
    ? nodes.filter(stateObserved).length
    : Number(explicitReached) || 0
  const replaySource = source.replay_paths || source.replay || {}
  const fallbackReplayFull = nodes.filter(item => normalizeInspectionReplayScope(item?.replay_scope || item?.replay_eligibility) === 'FULL').length
  const fallbackReplaySafePrefix = nodes.filter(item => normalizeInspectionReplayScope(item?.replay_scope || item?.replay_eligibility) === 'SAFE_PREFIX').length
  const replayFull = Number(replaySource.full ?? replaySource.full_paths ?? replaySource.full_path ?? stats.replay_full ?? fallbackReplayFull) || 0
  const replaySafePrefix = Number(replaySource.safe_prefix ?? replaySource.safe_prefixes ?? replaySource.prefix_to_safety_boundary ?? stats.replay_safe_prefix ?? fallbackReplaySafePrefix) || 0
  const replayVerified = Number(replaySource.verified ?? replaySource.verified_twice ?? stats.replay_verified ?? 0) || 0
  const replayObserved = Number(replaySource.observed ?? replaySource.observed_once ?? stats.replay_observed ?? 0) || 0
  const replayDiagnosticOnly = Number(replaySource.diagnostic_only ?? stats.replay_diagnostic_only ?? 0) || 0
  const explicitReplayTotal = replaySource.total ?? stats.replay_paths
  const replayTotal = explicitReplayTotal === null || explicitReplayTotal === undefined
    ? replayFull + replaySafePrefix
    : Number(explicitReplayTotal) || 0
  const explicitReplayCandidateCount = replaySource.candidate_count ?? stats.replay_candidate_count
  const replayCandidateCount = explicitReplayCandidateCount === null || explicitReplayCandidateCount === undefined
    ? replayTotal
    : Number(explicitReplayCandidateCount) || 0
  const replayDefaultSelectionLimit = Number(replaySource.default_selection_limit ?? stats.replay_default_selection_limit ?? 0) || 0
  const applicationFaults = Number(source.app_faults ?? stats.app_faults ?? run.fault_count ?? 0) || 0
  const infrastructureFaults = Number(source.infra_faults ?? stats.infra_faults ?? stats.infrastructure_faults ?? 0) || 0
  const automationFailures = Number(source.automation_failures ?? stats.automation_failures ?? 0) || 0
  const faults = Number(source.real_faults ?? (applicationFaults + infrastructureFaults)) || 0
  const attention = Number(
    source.attention_issues
      ?? source.requires_attention
      ?? stats.attention_issues
      ?? (faults + automationFailures),
  ) || 0
  const assessment = graph?.coverage_assessment || run?.coverage_assessment || {}
  const businessSource = source.business_coverage || assessment.summary || {}
  const businessCovered = Number(businessSource.covered_required ?? 0) || 0
  const businessTotal = Number(businessSource.total_required ?? 0) || 0
  const scopeSelected = Number(
    businessSource.scope_branches_selected
      ?? (Array.isArray(assessment.selected_branches) ? assessment.selected_branches.length : null)
      ?? businessSource.scope_branches_covered
      ?? 0,
  ) || 0
  const scopeComplete = Number(businessSource.scope_branches_covered ?? 0) || 0
  const scopeTotal = Number(businessSource.scope_branches_total ?? 2) || 2
  return {
    summaryAvailable,
    family: { total: familyTotal, expanded: familyExpanded, ratio: Math.max(0, Math.min(1, familyRatio)) },
    reached,
    replay: {
      total: replayTotal,
      full: replayFull,
      safePrefix: replaySafePrefix,
      verified: replayVerified,
      observed: replayObserved,
      diagnosticOnly: replayDiagnosticOnly,
      candidateCount: replayCandidateCount,
      defaultSelectionLimit: replayDefaultSelectionLimit,
    },
    faults,
    attention,
    issues: {
      application: applicationFaults,
      infrastructure: infrastructureFaults,
      automation: automationFailures,
    },
    business: {
      available: Boolean(businessTotal || assessment?.manifest || businessSource?.manifest),
      covered: businessCovered,
      total: businessTotal,
      ratio: businessTotal > 0 ? Math.max(0, Math.min(1, Number(businessSource.required_ratio ?? businessCovered / businessTotal))) : 0,
      weightedRatio: Number(businessSource.weighted_coverage ?? businessSource.required_ratio ?? 0) || 0,
      scopeSelected,
      scopeComplete,
      scopeTotal,
      evidenceQuality: String(businessSource.evidence_quality || 'UNKNOWN').toUpperCase(),
      selectedScopeVerdict: String(
        businessSource.selected_scope_verdict || assessment.selected_scope_verdict || 'NOT_EVALUATED',
      ).toUpperCase(),
      fullAppVerdict: String(
        businessSource.full_app_verdict || assessment.full_app_verdict || 'NOT_EVALUATED',
      ).toUpperCase(),
      branches: Array.isArray(assessment.branches) ? assessment.branches : [],
      blindSpots: Array.isArray(assessment.blind_spots) ? assessment.blind_spots : [],
      manifest: assessment.manifest || businessSource.manifest || {},
      origin: assessment.assessment_origin || '',
      surface: buildSurfaceCoverage(assessment.surface_coverage),
    },
  }
}

// Coverage against the cross-run application map.  Reported beside the manifest
// verdict and never merged with it: the manifest says whether the business
// journeys passed, this says out of how many screens, and which were missed.
const buildSurfaceCoverage = source => {
  const payload = source && typeof source === 'object' ? source : {}
  const known = Number(payload.package_known_surfaces) || 0
  const slots = payload.action_slots && typeof payload.action_slots === 'object'
    ? payload.action_slots
    : {}
  return {
    available: Boolean(payload.available) && known > 0,
    reason: String(payload.reason || ''),
    known,
    runVisited: Number(payload.run_visited_surfaces) || 0,
    runFullyCovered: Number(payload.run_fully_covered_surfaces) || 0,
    windowDays: Number(payload.cumulative_window_days) || 0,
    cumulativeCovered: Number(payload.cumulative_covered_surfaces) || 0,
    verdict: String(payload.cumulative_verdict || 'NOT_EVALUATED').toUpperCase(),
    unclassified: Number(payload.unclassified_surfaces) || 0,
    neverCovered: Array.isArray(payload.never_covered_surfaces)
      ? payload.never_covered_surfaces
      : [],
    stale: Array.isArray(payload.stale_surfaces) ? payload.stale_surfaces : [],
    slots: {
      total: Number(slots.total) || 0,
      coveredEver: Number(slots.covered_ever) || 0,
      coveredThisRun: Number(slots.covered_this_run) || 0,
      neverCovered: Number(slots.never_covered) || 0,
    },
  }
}

export const inspectionCoverageVerdictLabel = value => ({
  COMPLETE: '完整',
  PARTIAL: '部分覆盖',
  INCOMPLETE: '不完整',
  INCONCLUSIVE: '证据不足',
  NOT_IN_SCOPE: '不在本次范围',
  NOT_EVALUATED: '未评估',
  PENDING: '评估中',
}[String(value || '').trim().toUpperCase()] || value || '未评估')

export const inspectionCoverageItemStatusMeta = value => ({
  COVERED: { label: '已覆盖', type: 'success' },
  MISSING: { label: '缺失', type: 'danger' },
  INCONCLUSIVE: { label: '证据不足', type: 'warning' },
  NOT_IN_SCOPE: { label: '不在范围', type: 'info' },
}[String(value || '').trim().toUpperCase()] || { label: value || '未知', type: 'info' })

const LEGACY_COVERAGE_DETAIL_LABELS = Object.freeze({
  'home and four bottom-tab destinations have real transitions': '首页及四个底栏目的地均有真实 Transition 证据',
  'no HOME has XML labels plus real transitions to all four destinations': '缺少带完整底栏标签的首页，或未形成到四个目的地的真实 Transition',
  'matching states do not form the required real transition chain': '候选页面未形成该旅程要求的真实 Transition 链',
  'real transition chain matched': '已匹配该旅程自己的真实 Transition 链',
  'physical detail/specification/checkout/cashier transition chain missing': '缺少商品详情、规格、结算到收银台的完整真实 Transition 链',
  'real purchase chain (inline selected specification) reached cashier and final payment was blocked': '真实购买链已携带选中规格到达收银台，最终支付动作已安全拦截',
  'cashier reached, but no executed PAYMENT/BLOCKED boundary evidence': '已到达收银台，但缺少已执行的 BLOCKED/PAYMENT 安全边界证据',
  'state evidence matched': '已匹配可定位的页面证据',
  'no matching state/XML evidence': '未找到匹配的页面或 XML 证据',
})

const COVERAGE_REASON_CODE_LABELS = Object.freeze({
  BRANCH_NOT_SELECTED: '本次 Run 未选择该业务线或该旅程不适用',
  ENDPOINT_NOT_REVERIFIED: '终点未通过保留预算复验',
  EXECUTION_INCOMPLETE: '任务在预算或执行边界停止，无法判定该旅程缺失',
  PATH_MISSING: '未形成该旅程自己的真实 Transition 链',
  PAYMENT_BOUNDARY_MISSING: '已到达收银台，但缺少明确的 BLOCKED/PAYMENT 证据',
  UNKNOWN_OR_OPAQUE: '业务线包含未知或不透明页面，证据不足',
  V1_EVIDENCE_MISSING: '历史 v1 证据未满足该旅程判定条件',
  XML_MISSING: '页面 XML 缺失或不可读，无法完成判定',
})

export const inspectionCoverageItemReason = item => {
  const detail = String(item?.detail || '').trim()
  if (LEGACY_COVERAGE_DETAIL_LABELS[detail]) return LEGACY_COVERAGE_DETAIL_LABELS[detail]
  if (detail) return detail
  const code = String(item?.reason_code || '').trim().toUpperCase()
  return COVERAGE_REASON_CODE_LABELS[code] || code || '-'
}

export const inspectionEvidenceQualityLabel = value => ({
  HIGH: '高',
  MEDIUM: '中',
  LOW: '低',
  UNKNOWN: '未评估',
}[String(value || '').trim().toUpperCase()] || value || '未评估')

export const inspectionActionStatus = action => String(
  action?.failure_type
    || action?.final_status
    || action?.live_status
    || action?.invocation_status
    || action?.result
    || action?.status
    || 'PENDING',
).toUpperCase()

export const inspectionActionStatusMeta = actionOrStatus => {
  const status = typeof actionOrStatus === 'string'
    ? actionOrStatus.toUpperCase()
    : inspectionActionStatus(actionOrStatus)
  return INSPECTION_ACTION_STATUS_META[status] || { label: status || '未知', type: 'info' }
}

const PHASE_LABELS = Object.freeze({
  DISCOVERY: '探索页面',
  EXPLORE: '探索页面',
  EXPLORATION: '探索页面',
  RECOVERY: '恢复页面',
  RECOVER: '恢复页面',
  RECOVER_PARENT: '恢复页面',
  RECOVER_PEER: '恢复页面',
  REPLAY_ROOT: '恢复页面',
  ENTRY: '准备页面',
  PREPARE: '准备页面',
  STABILIZING: '等待页面稳定',
  ENTRY_SURVEY: '入口普查',
  COVERAGE_EXPLORE: '覆盖探索',
  REPRESENTATIVE_VERIFICATION: '代表验证',
  VERIFY: '验证稳定路径',
  VERIFICATION: '验证稳定路径',
  STABLE_PATH_VERIFICATION: '验证稳定路径',
  FINALIZE: '报告收尾',
  FINALIZING: '报告收尾',
  COMPLETE: '已完成',
})

export const inspectionPhaseLabel = phase => {
  const key = String(phase || '').trim().toUpperCase()
  return PHASE_LABELS[key] || String(phase || '等待巡检数据')
}

export const isInspectionVerificationPhase = phase => {
  const key = String(phase || '').trim().toUpperCase()
  return ['VERIFY', 'VERIFICATION', 'STABLE_PATH_VERIFICATION', 'VERIFY_STABLE_PATHS', 'REPRESENTATIVE_VERIFICATION', '验证稳定路径', '代表验证'].includes(key)
}

export const shouldClearInspectionActionOverlay = (phase, stage = '') => (
  isInspectionVerificationPhase(phase) || isInspectionVerificationPhase(stage)
)

export const inspectionLiveActionPanel = snapshot => {
  if (snapshot?.action_panel && typeof snapshot.action_panel === 'object') {
    return snapshot.action_panel
  }
  const page = snapshot?.page && typeof snapshot.page === 'object' ? snapshot.page : {}
  return {
    state_id: snapshot?.expansion_owner_state_id ?? page.state_id,
    expansion_epoch: snapshot?.expansion_epoch ?? 0,
    page,
    actions: Array.isArray(snapshot?.actions) ? snapshot.actions : [],
    current_action: snapshot?.current_action ?? null,
    canvas_matches_panel: snapshot?.canvas_matches_panel,
  }
}

export const inspectionLivePanelOwnerId = snapshot => {
  const panel = inspectionLiveActionPanel(snapshot)
  return panel?.state_id ?? panel?.page?.state_id ?? null
}

export const inspectionLivePanelEpoch = snapshot => {
  const panel = inspectionLiveActionPanel(snapshot)
  return Number(panel?.expansion_epoch ?? snapshot?.expansion_epoch ?? 0)
}

export const inspectionLiveCanvasMatchesPanel = snapshot => {
  const panel = inspectionLiveActionPanel(snapshot)
  const value = panel?.canvas_matches_panel
    ?? snapshot?.device_context?.canvas_matches_panel
    ?? snapshot?.canvas_matches_panel
  // Legacy snapshots predate the physical/logical split. Preserve their
  // previous overlay behavior until the server starts publishing this bit.
  return value === undefined || value === null ? true : Boolean(value)
}

export const mergeInspectionLiveSnapshot = (current, incoming) => {
  if (!incoming || typeof incoming !== 'object' || Array.isArray(incoming)) {
    return current || {}
  }
  const currentRunId = current?.run_id
  const incomingRunId = incoming?.run_id
  if (currentRunId != null
    && incomingRunId != null
    && String(currentRunId) !== String(incomingRunId)) {
    return { ...incoming }
  }

  const currentStreamId = current?.stream_id
  const incomingStreamId = incoming?.stream_id
  if (currentStreamId && incomingStreamId && currentStreamId !== incomingStreamId) {
    const currentStartedAt = Date.parse(current?.stream_started_at)
    const incomingStartedAt = Date.parse(incoming?.stream_started_at)
    if (Number.isFinite(currentStartedAt) && Number.isFinite(incomingStartedAt)) {
      if (incomingStartedAt <= currentStartedAt) return current || {}
      return { ...incoming }
    }
  }

  const currentRevision = Number(current?.revision)
  const incomingRevision = Number(incoming?.revision)
  if (Number.isFinite(currentRevision)
    && Number.isFinite(incomingRevision)
    && incomingRevision < currentRevision) return current || {}
  return { ...(current || {}), ...incoming }
}

export const inspectionFallbackImageReady = (url, loadedPath, currentPath) => (
  Boolean(url)
  && Boolean(loadedPath)
  && String(loadedPath) === String(currentPath || '')
)

export const INSPECTION_LIVE_SNAPSHOT_EVENT_TYPES = new Set([
  'RUN_STAGE', 'PHASE_CHANGED', 'FRONTIER_UPDATED', 'ACTION_DEFERRED',
  'ACTION_RESUMED', 'ACTION_COVERED_BY_FAMILY', 'ACTION_REBOUND',
  'ACTION_COVERED_BY_CONTRACT', 'ACTION_NAVIGATION_REUSED', 'ACTION_SAMPLED_OUT',
  'ACTION_STARTED', 'ACTION_INVOKED', 'ACTION_FINISHED',
])
