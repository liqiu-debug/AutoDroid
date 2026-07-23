const numberValue = value => {
  if (value === null || value === undefined || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

const stateIdOf = state => numberValue(state?.state_id ?? state?.id)
const transitionIdOf = transition => numberValue(transition?.id)
const sourceIdOf = transition => numberValue(transition?.source ?? transition?.from_state_id)
const targetIdOf = transition => numberValue(transition?.target ?? transition?.to_state_id)
const topologyTypeOf = transition => String(
  transition?.topology_type
    || (String(transition?.status || '').toUpperCase() === 'SELF_LOOP' ? 'SELF_LOOP' : ''),
).trim().toUpperCase()

const bySequence = (left, right) => (
  (numberValue(left?.sequence) ?? Number.MAX_SAFE_INTEGER)
  - (numberValue(right?.sequence) ?? Number.MAX_SAFE_INTEGER)
  || (transitionIdOf(left) ?? Number.MAX_SAFE_INTEGER)
  - (transitionIdOf(right) ?? Number.MAX_SAFE_INTEGER)
)

const byState = (left, right) => (
  (numberValue(left?.depth) ?? 0) - (numberValue(right?.depth) ?? 0)
  || (stateIdOf(left) ?? Number.MAX_SAFE_INTEGER) - (stateIdOf(right) ?? Number.MAX_SAFE_INTEGER)
)

const positiveNumber = value => {
  const number = numberValue(value)
  return number !== null && number > 0 ? number : null
}

export const assignInspectionDisplayLabels = (nodes = []) => {
  const ordered = [...nodes].sort((left, right) => (
    (stateIdOf(left) ?? Number.MAX_SAFE_INTEGER) - (stateIdOf(right) ?? Number.MAX_SAFE_INTEGER)
  ))
  const fallbackIndex = new Map(ordered.map((item, index) => [stateIdOf(item), index + 1]))
  const maximumIndex = Math.max(
    1,
    ...ordered.map((item, index) => positiveNumber(item?.display_index) ?? index + 1),
  )
  const padding = Math.max(3, String(maximumIndex).length)
  return nodes.map(item => {
    const displayIndex = positiveNumber(item?.display_index) ?? fallbackIndex.get(stateIdOf(item)) ?? 0
    const suppliedLabel = String(item?.display_label || '')
    const normalizedSuppliedLabel = /^P\d+$/i.test(suppliedLabel)
      ? `P${String(Number(suppliedLabel.slice(1))).padStart(padding, '0')}`
      : ''
    return {
      ...item,
      display_index: displayIndex,
      display_label: normalizedSuppliedLabel || `P${String(displayIndex).padStart(padding, '0')}`,
    }
  })
}

export const inspectionStateDisplayLabel = state => {
  const supplied = String(state?.display_label || '').trim().toUpperCase()
  if (/^P\d+$/.test(supplied)) {
    return `P${supplied.slice(1).padStart(3, '0')}`
  }
  return positiveNumber(state?.display_index)
    ? `P${String(state.display_index).padStart(3, '0')}`
    : 'P---'
}

const imageAspectRatio = (state, naturalSize = null) => {
  const width = positiveNumber(
    naturalSize?.width
      ?? state?.image_width
      ?? state?.original_width
      ?? state?.screenshot_width
  )
  const height = positiveNumber(
    naturalSize?.height
      ?? state?.image_height
      ?? state?.original_height
      ?? state?.screenshot_height
  )
  if (width && height) return width / height
  return positiveNumber(state?.image_aspect_ratio) || (9 / 20)
}

export const inspectionThumbnailSymbolSize = (
  state,
  naturalSize = null,
  maxWidth = 96,
  maxHeight = 112,
) => {
  const ratio = Math.min(8, Math.max(0.125, imageAspectRatio(state, naturalSize)))
  const width = ratio >= maxWidth / maxHeight ? maxWidth : maxHeight * ratio
  const height = ratio >= maxWidth / maxHeight ? maxWidth / ratio : maxHeight
  return [Number(width.toFixed(2)), Number(height.toFixed(2))]
}

const decodePathSegment = value => {
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

/**
 * Resolve every thumbnail reference into an authenticated API request.
 * The returned URL is only evidence for selecting an API method; callers
 * must never assign it directly to an image element.
 */
export const inspectionThumbnailAssetRequest = (state, runId = null) => {
  const assetId = state?.thumbnail_asset_id || state?.representative_thumbnail_asset_id
  if (assetId !== null && assetId !== undefined && assetId !== '') {
    return {
      key: `asset:${assetId}`,
      assetId: String(assetId),
      path: state?.thumbnail_path || state?.representative_thumbnail_path || null,
    }
  }

  const explicitPath = state?.thumbnail_path || state?.representative_thumbnail_path
  if (explicitPath) {
    return {
      key: `inspection:${runId ?? state?.run_id ?? ''}:${explicitPath}`,
      assetId: null,
      path: String(explicitPath),
    }
  }

  const rawUrl = String(state?.thumbnail_url || '').trim()
  if (!rawUrl) return null
  try {
    const parsed = new URL(rawUrl, 'http://inspection.local')
    const assetMatch = parsed.pathname.match(/^\/api\/assets\/([^/]+)$/)
    if (assetMatch) {
      const resolvedAssetId = decodePathSegment(assetMatch[1])
      return { key: `asset:${resolvedAssetId}`, assetId: resolvedAssetId, path: null }
    }
    if (/^\/api\/inspections\/runs\/[^/]+\/assets$/.test(parsed.pathname)) {
      const path = parsed.searchParams.get('path')
      if (path) {
        return {
          key: `inspection:${runId ?? state?.run_id ?? ''}:${path}`,
          assetId: null,
          path,
        }
      }
    }
  } catch {
    return null
  }
  return null
}

export const clampMindMapScale = (value, min = 0.02, max = 2.5) => (
  Math.max(min, Math.min(max, Number(value) || 1))
)

export const focusedMindMapScale = ({
  viewportWidth,
  minScale = 0.7,
  maxScale = 1,
} = {}) => clampMindMapScale(
  Math.max(1, Number(viewportWidth) || 1) / 1000,
  minScale,
  maxScale,
)

const ABNORMAL_INSPECTION_STATUSES = new Set(['FAIL', 'ERROR', 'ABORTED', 'CANCELLED'])
const NORMAL_INSPECTION_STATUSES = new Set(['PASS', 'WARNING', 'SUCCESS', 'COMPLETED'])
const ABNORMAL_TERMINAL_OUTCOMES = new Set([
  'APP_FAULT',
  'AUTOMATION_FAILED',
  'INFRA_FAULT',
  'LOCATOR_FAILED',
  'BUDGET_STOP',
  'CANCELLED',
])

export const inspectionMindMapInitialFocusId = ({
  status,
  terminalOutcome,
  lastActiveStateId,
  currentStateId,
  homeStateId,
} = {}) => {
  const normalizedStatus = String(status || '').trim().toUpperCase()
  const normalizedOutcome = String(terminalOutcome || '').trim().toUpperCase()
  const abnormal = ABNORMAL_INSPECTION_STATUSES.has(normalizedStatus)
    || ABNORMAL_TERMINAL_OUTCOMES.has(normalizedOutcome)
  const terminal = abnormal || NORMAL_INSPECTION_STATUSES.has(normalizedStatus)
  const stateId = !terminal
    ? positiveNumber(currentStateId) || positiveNumber(lastActiveStateId) || positiveNumber(homeStateId)
    : abnormal
      ? positiveNumber(lastActiveStateId) || positiveNumber(currentStateId) || positiveNumber(homeStateId)
      : positiveNumber(homeStateId) || positiveNumber(lastActiveStateId) || positiveNumber(currentStateId)
  return stateId ? `state-${stateId}` : ''
}

export const fitMindMapViewport = ({
  viewportWidth,
  viewportHeight,
  contentWidth,
  contentHeight,
  padding = 24,
  minScale = 0.02,
  maxScale = 2.5,
} = {}) => {
  const availableWidth = Math.max(1, Number(viewportWidth) - padding * 2)
  const availableHeight = Math.max(1, Number(viewportHeight) - padding * 2)
  const width = Math.max(1, Number(contentWidth) || 1)
  const height = Math.max(1, Number(contentHeight) || 1)
  const scale = clampMindMapScale(
    Math.min(availableWidth / width, availableHeight / height),
    minScale,
    maxScale,
  )
  const scaledWidth = width * scale
  const scaledHeight = height * scale
  return {
    scale,
    scrollLeft: Math.max(0, (scaledWidth - Number(viewportWidth || 0)) / 2),
    scrollTop: Math.max(0, (scaledHeight - Number(viewportHeight || 0)) / 2),
  }
}

export const zoomMindMapViewport = ({
  scale,
  factor,
  scrollLeft,
  scrollTop,
  viewportWidth,
  viewportHeight,
  contentWidth,
  contentHeight,
  minScale = 0.02,
  maxScale = 2.5,
} = {}) => {
  const previousScale = clampMindMapScale(scale, minScale, maxScale)
  const nextScale = clampMindMapScale(previousScale * Number(factor || 1), minScale, maxScale)
  const centerX = (Number(scrollLeft || 0) + Number(viewportWidth || 0) / 2) / previousScale
  const centerY = (Number(scrollTop || 0) + Number(viewportHeight || 0) / 2) / previousScale
  const maxScrollLeft = Math.max(0, Number(contentWidth || 0) * nextScale - Number(viewportWidth || 0))
  const maxScrollTop = Math.max(0, Number(contentHeight || 0) * nextScale - Number(viewportHeight || 0))
  return {
    scale: nextScale,
    scrollLeft: Math.max(0, Math.min(maxScrollLeft, centerX * nextScale - Number(viewportWidth || 0) / 2)),
    scrollTop: Math.max(0, Math.min(maxScrollTop, centerY * nextScale - Number(viewportHeight || 0) / 2)),
  }
}

export const centerMindMapViewport = ({
  pointX,
  pointY,
  scale,
  viewportWidth,
  viewportHeight,
  contentWidth,
  contentHeight,
} = {}) => {
  const resolvedScale = clampMindMapScale(scale)
  const maxScrollLeft = Math.max(0, Number(contentWidth || 0) * resolvedScale - Number(viewportWidth || 0))
  const maxScrollTop = Math.max(0, Number(contentHeight || 0) * resolvedScale - Number(viewportHeight || 0))
  return {
    scrollLeft: Math.max(0, Math.min(
      maxScrollLeft,
      Number(pointX || 0) * resolvedScale - Number(viewportWidth || 0) / 2,
    )),
    scrollTop: Math.max(0, Math.min(
      maxScrollTop,
      Number(pointY || 0) * resolvedScale - Number(viewportHeight || 0) / 2,
    )),
  }
}

export const mindMapScrollOptions = (
  { scrollLeft = 0, scrollTop = 0 } = {},
  behavior = 'auto',
) => ({
  left: Number(scrollLeft) || 0,
  top: Number(scrollTop) || 0,
  behavior,
})

export const boundedMindMapExportSize = ({
  width,
  height,
  pixelRatio = 2,
  maxSide = 4096,
  maxPixels = 12_000_000,
} = {}) => {
  const sourceWidth = Math.max(1, Number(width) || 1)
  const sourceHeight = Math.max(1, Number(height) || 1)
  const desiredScale = Math.max(0.1, Number(pixelRatio) || 1)
  const limitScale = Math.min(
    desiredScale,
    maxSide / sourceWidth,
    maxSide / sourceHeight,
    Math.sqrt(maxPixels / (sourceWidth * sourceHeight)),
  )
  return {
    width: Math.max(1, Math.round(sourceWidth * limitScale)),
    height: Math.max(1, Math.round(sourceHeight * limitScale)),
    scale: limitScale,
  }
}

/**
 * ECharts' LR tree layout places depth on the horizontal axis and distributes
 * leaves vertically, with parent nodes centered over their children. Mirror
 * that documented layout here so viewport navigation does not depend on the
 * private ECharts model API.
 */
export const inspectionMindMapNodePositions = (root, {
  width,
  height,
  left = 28,
  right = 150,
  top = 36,
  bottom = 36,
} = {}) => {
  const positions = new Map()
  if (!root) return positions
  const leaves = []
  let maxDepth = 0
  const visitLeaves = (node, depth = 0, parentId = '') => {
    maxDepth = Math.max(maxDepth, depth)
    const children = node?.children || []
    if (!children.length) leaves.push({ node, parentId })
    children.forEach(child => visitLeaves(child, depth + 1, node.id))
  }
  visitLeaves(root)

  let cursor = 0
  leaves.forEach((leaf, index) => {
    if (index > 0) cursor += leaves[index - 1].parentId === leaf.parentId ? 1 : 2
    leaf.unit = cursor
  })
  const outerGap = leaves.length > 1 && leaves[0].parentId !== leaves[leaves.length - 1].parentId ? 1 : 0.5
  const denominator = Math.max(1, cursor + outerGap * 2)
  const availableWidth = Math.max(1, Number(width || 0) - left - right)
  const availableHeight = Math.max(1, Number(height || 0) - top - bottom)
  const leafUnits = new Map(leaves.map(leaf => [leaf.node.id, leaf.unit]))

  const place = (node, depth = 0) => {
    const children = node?.children || []
    const childUnits = children.map(child => place(child, depth + 1))
    const unit = childUnits.length
      ? childUnits.reduce((total, value) => total + value, 0) / childUnits.length
      : leafUnits.get(node.id) || 0
    positions.set(node.id, {
      x: left + (maxDepth ? depth * availableWidth / maxDepth : availableWidth / 2),
      y: top + (unit + outerGap) * availableHeight / denominator,
    })
    return unit
  }
  place(root)
  return positions
}

const stableObject = value => {
  if (Array.isArray(value)) return value.map(stableObject)
  if (!value || typeof value !== 'object') return value
  return Object.fromEntries(Object.keys(value).sort().map(key => [key, stableObject(value[key])]))
}

const normalizedSignatureValue = value => String(value || 'NONE').trim().toUpperCase() || 'NONE'

const booleanValue = value => value === true || ['1', 'TRUE', 'YES'].includes(
  String(value || '').trim().toUpperCase(),
)

const terminalBoundarySignature = boundary => ({
  terminal_outcome: normalizedSignatureValue(
    boundary?.terminal_outcome || boundary?.boundary_type,
  ),
  risk_type: normalizedSignatureValue(
    boundary?.risk_type || boundary?.action?.risk_type,
  ),
  action_role: normalizedSignatureValue(
    boundary?.action_role
      || boundary?.action?.action_role
      || boundary?.action?.action_role_key,
  ),
  boundary_evidence: normalizedSignatureValue(boundary?.boundary_evidence),
  attention_required: booleanValue(boundary?.attention_required),
})

export const inspectionMindMapAggregationSignature = node => {
  if (node?.kind !== 'state') return null
  if (['home', 'viewport', 'orphan'].includes(String(node.payload?.tree_role || '').toLowerCase())) return null
  const familyId = node.payload?.exploration_family_id ?? node.payload?.family_id
  if (familyId === null || familyId === undefined || familyId === '') return null
  const payload = node.payload || {}
  const suppliedBoundaries = Array.isArray(payload.terminal_boundaries)
    ? payload.terminal_boundaries
    : []
  const boundaries = (suppliedBoundaries.length
    ? suppliedBoundaries.map(terminalBoundarySignature)
    : [{
        terminal_outcome: normalizedSignatureValue(payload.terminal_outcome),
        risk_type: normalizedSignatureValue(payload.risk_type),
        action_role: normalizedSignatureValue(payload.action_role || payload.action_role_key),
        boundary_evidence: normalizedSignatureValue(payload.boundary_evidence),
        attention_required: booleanValue(payload.attention_required),
      }]
  ).sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right)))
  return stableObject({
    family_id: String(familyId),
    page_subtype: normalizedSignatureValue(payload.page_subtype || 'UNKNOWN'),
    page_role: normalizedSignatureValue(payload.page_role || payload.template_role || 'UNKNOWN'),
    terminal_boundaries: boundaries,
  })
}

const aggregationKey = node => {
  const signature = inspectionMindMapAggregationSignature(node)
  return signature ? JSON.stringify(signature) : null
}

export const inspectionMindMapNodeNeedsAttention = node => {
  if (!node) return false
  if (booleanValue(node.attention_required)) return true
  if ((node.terminal_boundaries || []).some(item => booleanValue(item?.attention_required))) return true
  if (node.is_dynamic || node.is_opaque) return true
  if (String(node.locator_quality || '').toUpperCase() === 'COORDINATE_ONLY') return true
  if (['UNSTABLE', 'UNKNOWN'].includes(String(node.reachability_evidence || '').toUpperCase())) return true
  return ['NONE', 'DIAGNOSTIC_ONLY'].includes(String(
    node.replay_scope || node.replay_eligibility || '',
  ).toUpperCase())
}

/**
 * Compact only confidently equivalent siblings. Descendants from every
 * member remain visible below the representative, so the key-path view never
 * hides a unique downstream page.
 */
export const aggregateInspectionMindMap = (root, mode = 'all') => {
  if (mode !== 'key') return root
  const visit = node => {
    const visitedChildren = (node?.children || []).map(visit)
    const groups = new Map()
    const ordered = []
    visitedChildren.forEach(child => {
      const key = aggregationKey(child)
      if (!key) {
        ordered.push({ key: null, children: [child] })
        return
      }
      let group = groups.get(key)
      if (!group) {
        group = { key, children: [] }
        groups.set(key, group)
        ordered.push(group)
      }
      group.children.push(child)
    })
    const children = ordered.map(group => {
      if (!group.key || group.children.length === 1) return group.children[0]
      const representative = group.children[0]
      const mergedChildren = group.children.flatMap(item => item.children || [])
      const recursivelyMerged = visit({ children: mergedChildren }).children
      const aggregatedStateIds = group.children.flatMap(item => (
        item.payload?.aggregated_state_ids || [item.payload?.state_id]
      )).filter(item => item !== null && item !== undefined)
      return {
        ...representative,
        name: `${representative.name} · 同类页面 +${group.children.length - 1}`,
        payload: {
          ...representative.payload,
          aggregated_count: group.children.length,
          aggregated_state_ids: aggregatedStateIds,
        },
        children: recursivelyMerged,
      }
    })
    return { ...node, children }
  }
  return visit(root)
}

export const PAGE_TREE_LINE_STYLE = Object.freeze({
  color: '#a8b0bc',
  type: 'solid',
  width: 1.2,
  opacity: 1,
})

const pageTreeLineStyle = () => ({ ...PAGE_TREE_LINE_STYLE })

export const branchLabel = branch => {
  if (branch === 'guest') return '未登录'
  if (branch === 'authenticated') return '已登录'
  return branch || '未分组'
}

export const truncateMindMapLabel = (value, limit = 28) => {
  const text = String(value || '').replace(/\s+/g, ' ').trim()
  if (text.length <= limit) return text
  return `${text.slice(0, Math.max(1, limit - 1))}…`
}

export const actionTargetLabel = transition => {
  const meta = transition?.target_meta || {}
  const actionType = String(transition?.action_type || '').toLowerCase()
  const actionRole = String(
    transition?.action_role || transition?.action_role_key || '',
  ).toUpperCase()
  const semanticLabel = [meta.content_desc, meta.text]
    .map(value => String(value || '').replace(/\s+/g, ' ').trim())
    .find(value => value && !/^(?:android|androidx|com\.)\./i.test(value))
  if (semanticLabel) return truncateMindMapLabel(semanticLabel, 28)
  if (actionType === 'scroll' || actionType === 'swipe' || actionRole.startsWith('SCROLL:')) {
    return '页面滚动'
  }
  if (
    actionRole.includes('VISUAL')
    || transition?.visual_locator_evidence
    || transition?.coordinate_only
  ) return '图片入口'
  const className = String(meta.class || '')
  if (/ScrollView|RecyclerView/i.test(className)) return '页面滚动'
  if (/EditText|TextInput/i.test(className)) return '输入框'
  if (/Button/i.test(className)) return '按钮'
  if (/ImageView/i.test(className)) return '图片入口'
  if (/^(?:android|androidx|com\.)\./i.test(className)) return '未命名页面入口'
  const reason = String(transition?.reason || '').trim()
  if (reason) return truncateMindMapLabel(reason, 28)
  const actionKey = String(transition?.action_key || '').trim()
  const safeActionKey = (
    actionKey
    && !/^\/?\//.test(actionKey)
    && !/^[a-f\d]{16,}$/i.test(actionKey)
  ) ? actionKey : ''
  return truncateMindMapLabel(
    safeActionKey || '未命名页面入口',
    28,
  )
}

export const pageActionSummary = transition => {
  const sequence = numberValue(transition?.sequence)
  const prefix = sequence === null ? '' : `#${sequence} `
  return `${prefix}${transition?.action_type || '操作'} · ${actionTargetLabel(transition)}`
}

const stateLabel = (state, {
  branchKey = '',
  level = 0,
  root = false,
  orphan = false,
  viewport = false,
  incomingTransition = null,
} = {}) => {
  const displayLabel = inspectionStateDisplayLabel(state)
  const explorationSuffix = String(state?.exploration_mode || '').toUpperCase() === 'FULL'
    ? ' · 代表'
    : String(state?.exploration_mode || '').toUpperCase() === 'DELTA_ONLY' ? ' · 增量' : ''
  if (orphan) return `未关联页面 · ${displayLabel}`
  if (viewport) return `同页视口 · ${displayLabel}`
  if (root) return `${branchLabel(branchKey)}首页 · ${displayLabel}${explorationSuffix}`
  const entryLabel = incomingTransition
    ? truncateMindMapLabel(actionTargetLabel(incomingTransition), 8)
    : ''
  const pageTitle = truncateMindMapLabel(
    state?.page_title || state?.title || state?.display_title || '',
    12,
  )
  if (pageTitle) return `${pageTitle} · ${displayLabel}${explorationSuffix}`
  return entryLabel && entryLabel !== '-'
    ? `${entryLabel} · ${displayLabel}${explorationSuffix}`
    : `第 ${level + 1} 层 · ${displayLabel}${explorationSuffix}`
}

const statusStyle = transition => {
  const status = String(transition?.failure_type || transition?.status || '').toUpperCase()
  if (status === 'BLOCKED') return { color: '#fef0f0', borderColor: '#f56c6c', borderType: 'dashed' }
  if (['ERROR', 'ACTION_ERROR'].includes(status)) return { color: '#fef0f0', borderColor: '#d03050' }
  if (['LOCATOR_DRIFT', 'AMBIGUOUS', 'LOCATOR_AMBIGUOUS', 'LOCATOR_NOT_FOUND', 'COORDINATE_ONLY', 'COORDINATE_UNSAFE', 'COORDINATE_STALE', 'PARENT_RECOVERY_FAILED', 'PARENT_RECOVERY_CASCADE', 'PATH_DIVERGED'].includes(status) || transition?.coordinate_only) {
    return { color: '#fdf6ec', borderColor: '#e6a23c', borderType: 'dashed' }
  }
  if (['PASS', 'SELF_LOOP'].includes(status)) return { color: '#f0f9eb', borderColor: '#67c23a' }
  return { color: '#f4f4f5', borderColor: '#909399' }
}

const stateStyle = state => {
  if (state?.tree_role === 'home') {
    return { color: '#ecf5ff', borderColor: '#409eff', borderWidth: 2 }
  }
  if (state?.tree_role === 'orphan') {
    return { color: '#fdf6ec', borderColor: '#e6a23c', borderWidth: 2, borderType: 'dashed' }
  }
  const stability = String(state?.stable_status || '').toUpperCase()
  if (stability === 'STABLE') return { color: '#f0f9eb', borderColor: '#67c23a', borderWidth: 2 }
  if (stability === 'VIEWPORT') return { color: '#f0f9eb', borderColor: '#67c23a', borderWidth: 2 }
  return { color: '#ffffff', borderColor: '#c0c4cc', borderWidth: 1 }
}

const makeNode = ({
  id,
  name,
  kind,
  level,
  payload = null,
  children = [],
  incomingTransition = null,
  lineStyle = null,
}) => {
  if (kind === 'virtual-root') {
    return {
      id,
      name: '',
      kind,
      level: -1,
      payload,
      children,
      symbol: 'circle',
      symbolSize: 1,
      itemStyle: { color: 'transparent', borderColor: 'transparent', opacity: 0 },
      label: { show: false },
    }
  }
  const isState = kind === 'state'
  const isViewport = isState && payload?.tree_role === 'viewport'
  return {
    id,
    name,
    kind,
    level,
    payload,
    children,
    incomingTransition,
    incomingLabel: incomingTransition ? pageActionSummary(incomingTransition) : '',
    symbol: isState ? 'roundRect' : kind === 'reference' ? 'emptyCircle' : 'diamond',
    symbolSize: isState ? (isViewport ? [64, 28] : [72, 34]) : kind === 'reference' ? 12 : 15,
    itemStyle: isState ? stateStyle(payload || {}) : statusStyle(payload?.transition || payload || {}),
    ...(lineStyle ? { lineStyle } : {}),
  }
}

const isMeaningfulTarget = transition => {
  const sourceId = sourceIdOf(transition)
  const targetId = targetIdOf(transition)
  if (targetId === null) return false
  if (sourceId !== targetId) return true
  return topologyTypeOf(transition) === 'SELF_LOOP'
    || String(transition?.status || '').toUpperCase() === 'SELF_LOOP'
}

const selectCanonicalIncoming = (state, stateMap, transitionsById, inboundByState) => {
  const stateId = stateIdOf(state)
  const parentId = numberValue(state?.parent_state_id)
  const incomingId = numberValue(state?.incoming_transition_id)
  const hasParentField = Object.prototype.hasOwnProperty.call(state || {}, 'parent_state_id')

  if (parentId === null && hasParentField) return null
  const explicit = incomingId === null ? null : transitionsById.get(incomingId)
  if (
    explicit
    && targetIdOf(explicit) === stateId
    && sourceIdOf(explicit) !== stateId
    && (parentId === null || sourceIdOf(explicit) === parentId)
  ) return explicit

  const inbound = (inboundByState.get(stateId) || [])
    .filter(item => sourceIdOf(item) !== stateId && stateMap.has(sourceIdOf(item)))
    .sort(bySequence)
  if (parentId !== null) return inbound.find(item => sourceIdOf(item) === parentId) || null
  return hasParentField ? null : inbound[0] || null
}

const hierarchyRoleOf = state => String(state?.hierarchy_role || '').trim().toUpperCase()
const relationTypeOf = transition => String(transition?.relation_type || '').trim().toUpperCase()

const selectHierarchyIncoming = (state, stateMap, transitionsById, inboundByState) => {
  const stateId = stateIdOf(state)
  const role = hierarchyRoleOf(state)
  if (role === 'BRANCH_ROOT') return null

  const incomingId = numberValue(state?.incoming_transition_id)
  const explicit = incomingId === null ? null : transitionsById.get(incomingId)
  if (
    explicit
    && targetIdOf(explicit) === stateId
    && sourceIdOf(explicit) !== stateId
    && stateMap.has(sourceIdOf(explicit))
  ) return explicit

  const inbound = (inboundByState.get(stateId) || [])
    .filter(item => sourceIdOf(item) !== stateId && stateMap.has(sourceIdOf(item)))
    .sort(bySequence)
  if (role === 'PEER') return inbound.find(item => relationTypeOf(item) === 'PEER') || null
  if (role === 'VIEWPORT') {
    return inbound.find(item => relationTypeOf(item) === 'VIEWPORT')
      || inbound.find(item => String(item?.action_type || '').trim().toLowerCase() === 'scroll')
      || null
  }

  const parentId = numberValue(state?.parent_state_id)
  if (parentId !== null) {
    return inbound.find(item => sourceIdOf(item) === parentId)
      || inbound.find(item => relationTypeOf(item) === 'CHILD')
      || inbound.find(item => relationTypeOf(item) === 'PEER')
      || null
  }
  return inbound.find(item => relationTypeOf(item) === 'PEER') || inbound[0] || null
}

const referenceLabel = (stateId, transition, stateMap = null) => {
  const traversalCount = Math.max(1, numberValue(transition?.traversal_count) ?? 1)
  const targetLabel = inspectionStateDisplayLabel(stateMap?.get(stateId))
  if (['SELF_LOOP', 'CYCLE_BACK', 'REVISIT'].includes(topologyTypeOf(transition))) {
    return `回到 ${targetLabel} · ${traversalCount} 次`
  }
  return `引用 ${targetLabel} · ${pageActionSummary(transition)}`
}

const referenceNode = (stateId, level, transition, stateMap = null) => makeNode({
  id: `reference-${transitionIdOf(transition) ?? 'state'}-${stateId}`,
  name: referenceLabel(stateId, transition, stateMap),
  kind: 'reference',
  level,
  payload: { state_id: stateId, transition },
  lineStyle: pageTreeLineStyle(),
})

const buildPersistedHierarchyRoots = ({
  branchKey,
  branchStates,
  branchTransitions,
  transitionsById,
}) => {
  const stateMap = new Map(branchStates.map(item => [stateIdOf(item), item]))
  const outgoingByState = new Map()
  const inboundByState = new Map()
  branchTransitions.forEach(item => {
    const sourceId = sourceIdOf(item)
    const targetId = targetIdOf(item)
    if (!outgoingByState.has(sourceId)) outgoingByState.set(sourceId, [])
    outgoingByState.get(sourceId).push(item)
    if (targetId !== null) {
      if (!inboundByState.has(targetId)) inboundByState.set(targetId, [])
      inboundByState.get(targetId).push(item)
    }
  })

  const canonicalByChild = new Map()
  const canonicalChildByTransition = new Map()
  branchStates.forEach(state => {
    const incoming = selectHierarchyIncoming(state, stateMap, transitionsById, inboundByState)
    if (!incoming) return
    canonicalByChild.set(stateIdOf(state), incoming)
    canonicalChildByTransition.set(transitionIdOf(incoming), stateIdOf(state))
  })

  const viewportStateIds = new Set()
  branchStates.forEach(state => {
    const stateId = stateIdOf(state)
    const incoming = canonicalByChild.get(stateId)
    const source = incoming ? stateMap.get(sourceIdOf(incoming)) : null
    const sameDepthScroll = (
      String(incoming?.action_type || '').trim().toLowerCase() === 'scroll'
      && source
      && numberValue(source.depth) !== null
      && numberValue(source.depth) === numberValue(state.depth)
    )
    if (
      hierarchyRoleOf(state) === 'VIEWPORT'
      || String(state?.stable_status || '').toUpperCase() === 'VIEWPORT'
      || sameDepthScroll
    ) viewportStateIds.add(stateId)
  })

  const roleFor = state => {
    const persisted = hierarchyRoleOf(state)
    if (persisted) return persisted
    const stateId = stateIdOf(state)
    if (viewportStateIds.has(stateId)) return 'VIEWPORT'
    const incoming = canonicalByChild.get(stateId)
    if (relationTypeOf(incoming) === 'PEER') return 'PEER'
    if (numberValue(state?.parent_state_id) === null && !incoming) return 'BRANCH_ROOT'
    return 'PAGE'
  }

  const childrenByParent = new Map()
  branchStates.forEach(state => {
    const stateId = stateIdOf(state)
    const parentId = numberValue(state?.parent_state_id)
    if (parentId === null || parentId === stateId || !stateMap.has(parentId)) return
    if (!childrenByParent.has(parentId)) childrenByParent.set(parentId, [])
    childrenByParent.get(parentId).push(state)
  })
  childrenByParent.forEach(children => children.sort((left, right) => {
    const leftIncoming = canonicalByChild.get(stateIdOf(left))
    const rightIncoming = canonicalByChild.get(stateIdOf(right))
    const incomingOrder = bySequence(leftIncoming, rightIncoming)
    return incomingOrder || byState(left, right)
  }))

  const nonNavigationActionsOf = stateId => (
    (outgoingByState.get(stateId) || []).filter(transition => {
      const canonicalChildId = canonicalChildByTransition.get(transitionIdOf(transition))
      if (canonicalChildId !== undefined && stateMap.has(canonicalChildId)) return false
      const targetId = targetIdOf(transition)
      return !(isMeaningfulTarget(transition) && targetId !== null && stateMap.has(targetId))
    })
  )

  const referenceNodesOf = (stateId, level) => (
    (outgoingByState.get(stateId) || [])
      .sort(bySequence)
      .filter(transition => {
        if (canonicalChildByTransition.has(transitionIdOf(transition))) return false
        // Tab switches are represented by peer page nodes at their logical
        // level. Drawing the real click again would make the peer look like a
        // child or a cross-page duplicate.
        if (relationTypeOf(transition) === 'PEER') return false
        const targetId = targetIdOf(transition)
        return isMeaningfulTarget(transition) && targetId !== null && stateMap.has(targetId)
      })
      .map(transition => referenceNode(targetIdOf(transition), level, transition, stateMap))
  )

  const rendered = new Set()
  const buildViewport = (stateId, pageLevel, transition) => {
    const state = stateMap.get(stateId)
    return makeNode({
      id: `state-${stateId}`,
      name: stateLabel(state, { level: pageLevel, viewport: true }),
      kind: 'state',
      level: pageLevel,
      payload: {
        ...state,
        tree_role: 'viewport',
        viewport_auxiliary: true,
        non_navigation_actions: nonNavigationActionsOf(stateId),
      },
      children: [],
      incomingTransition: transition || null,
      lineStyle: pageTreeLineStyle(),
    })
  }

  const flattenedViewportChildren = (
    viewportId,
    pageLevel,
    ancestors,
    viewportAncestors,
  ) => {
    const children = []
    const nextViewportAncestors = new Set(viewportAncestors)
    nextViewportAncestors.add(viewportId)

    ;(childrenByParent.get(viewportId) || []).forEach(childState => {
      const childId = stateIdOf(childState)
      const incoming = canonicalByChild.get(childId) || null
      if (viewportStateIds.has(childId)) {
        if (
          nextViewportAncestors.has(childId)
          || ancestors.has(childId)
          || rendered.has(childId)
        ) {
          if (incoming) children.push(referenceNode(childId, pageLevel + 1, incoming, stateMap))
          return
        }
        rendered.add(childId)
        children.push(buildViewport(childId, pageLevel, incoming))
        children.push(...flattenedViewportChildren(
          childId,
          pageLevel,
          ancestors,
          nextViewportAncestors,
        ))
        return
      }
      children.push(buildState(childId, pageLevel + 1, ancestors, { incomingTransition: incoming }))
    })
    children.push(...referenceNodesOf(viewportId, pageLevel + 1))
    return children
  }

  const buildState = (stateId, level, ancestors = new Set(), options = {}) => {
    const state = stateMap.get(stateId)
    if (!state) return referenceNode(stateId, level, options.incomingTransition, stateMap)
    if (ancestors.has(stateId) || rendered.has(stateId)) {
      return referenceNode(stateId, level, options.incomingTransition, stateMap)
    }
    rendered.add(stateId)
    const nextAncestors = new Set(ancestors)
    nextAncestors.add(stateId)

    const children = []
    ;(childrenByParent.get(stateId) || []).forEach(childState => {
      const childId = stateIdOf(childState)
      const incoming = canonicalByChild.get(childId) || null
      if (viewportStateIds.has(childId)) {
        if (nextAncestors.has(childId) || rendered.has(childId)) {
          if (incoming) children.push(referenceNode(childId, level + 1, incoming, stateMap))
          return
        }
        rendered.add(childId)
        children.push(buildViewport(childId, level, incoming))
        children.push(...flattenedViewportChildren(childId, level, nextAncestors, new Set()))
        return
      }
      children.push(buildState(childId, level + 1, nextAncestors, { incomingTransition: incoming }))
    })
    children.push(...referenceNodesOf(stateId, level + 1))

    const role = roleFor(state)
    const orphan = Boolean(options.orphan || role === 'ORPHAN')
    const root = Boolean(options.root || role === 'BRANCH_ROOT')
    const treeRole = root ? 'home' : orphan ? 'orphan' : role === 'PEER' ? 'peer' : 'page'
    const incomingTransition = root ? null : options.incomingTransition || canonicalByChild.get(stateId) || null
    return makeNode({
      id: `state-${stateId}`,
      name: stateLabel(state, {
        branchKey,
        level,
        root,
        orphan,
        incomingTransition,
      }),
      kind: 'state',
      level,
      payload: {
        ...state,
        orphan,
        tree_role: treeRole,
        non_navigation_actions: nonNavigationActionsOf(stateId),
      },
      children,
      incomingTransition,
      lineStyle: pageTreeLineStyle(),
    })
  }

  const declaredRoots = branchStates.filter(state => roleFor(state) === 'BRANCH_ROOT')
  const declaredRootIds = new Set(declaredRoots.map(stateIdOf))
  const hasExplicitRoles = branchStates.some(state => hierarchyRoleOf(state))
  const topLevelStates = branchStates.filter(state => (
    numberValue(state?.parent_state_id) === null
    && roleFor(state) !== 'ORPHAN'
  ))
  const fallbackRoot = declaredRoots.length || hasExplicitRoles
    ? null
    : topLevelStates.find(state => !canonicalByChild.has(stateIdOf(state)) && roleFor(state) !== 'PEER')
      || topLevelStates[0]
      || null
  if (fallbackRoot) declaredRootIds.add(stateIdOf(fallbackRoot))

  const orderedTopLevel = [
    ...branchStates.filter(state => declaredRootIds.has(stateIdOf(state))).sort(byState),
    ...topLevelStates.filter(state => !declaredRootIds.has(stateIdOf(state))).sort(byState),
  ]
  const roots = orderedTopLevel.map(state => buildState(
    stateIdOf(state),
    0,
    new Set(),
    { root: declaredRootIds.has(stateIdOf(state)) },
  ))
  branchStates.forEach(state => {
    const stateId = stateIdOf(state)
    if (!rendered.has(stateId)) roots.push(buildState(stateId, 0, new Set(), { orphan: true }))
  })
  return roots
}

/**
 * Build a page-only hierarchy. Canonical discovery transitions connect page
 * nodes directly and non-canonical targets become finite references. Actions
 * without a child page stay in the state payload for its detail/action-map
 * view instead of crowding the hierarchy with hundreds of leaves.
 */
export const buildInspectionMindMap = ({
  runId,
  hierarchyVersion = 1,
  nodes = [],
  links = [],
}) => {
  const displayNodes = assignInspectionDisplayLabels(nodes)
  const usePersistedHierarchy = (numberValue(hierarchyVersion) ?? 1) >= 2
  const transitions = [...links].sort(bySequence)
  const transitionsById = new Map(transitions.map(item => [transitionIdOf(item), item]))
  const branchKeys = [...new Set(displayNodes.map(item => item.branch_key || 'unknown'))]
  const pageRoots = []

  branchKeys.forEach(branchKey => {
    const branchStates = displayNodes.filter(item => (item.branch_key || 'unknown') === branchKey).sort(byState)
    const stateMap = new Map(branchStates.map(item => [stateIdOf(item), item]))
    const branchTransitions = transitions.filter(item => stateMap.has(sourceIdOf(item)))
    if (usePersistedHierarchy) {
      pageRoots.push(...buildPersistedHierarchyRoots({
        branchKey,
        branchStates,
        branchTransitions,
        transitionsById,
      }))
      return
    }
    const outgoingByState = new Map()
    const inboundByState = new Map()
    branchTransitions.forEach(item => {
      const sourceId = sourceIdOf(item)
      const targetId = targetIdOf(item)
      if (!outgoingByState.has(sourceId)) outgoingByState.set(sourceId, [])
      outgoingByState.get(sourceId).push(item)
      if (targetId !== null) {
        if (!inboundByState.has(targetId)) inboundByState.set(targetId, [])
        inboundByState.get(targetId).push(item)
      }
    })

    const canonicalByChild = new Map()
    const canonicalChildByTransition = new Map()
    branchStates.forEach(state => {
      const selected = selectCanonicalIncoming(state, stateMap, transitionsById, inboundByState)
      if (!selected) return
      canonicalByChild.set(stateIdOf(state), selected)
      canonicalChildByTransition.set(transitionIdOf(selected), stateIdOf(state))
    })

    // A scroll capture represents another viewport of the same business page,
    // not another business page. Prefer the persisted VIEWPORT classification,
    // while also covering live/polling data where the status has not been
    // committed yet but the canonical scroll keeps the same business depth.
    const viewportStateIds = new Set()
    branchStates.forEach(state => {
      const stateId = stateIdOf(state)
      const incoming = canonicalByChild.get(stateId)
      const parent = incoming ? stateMap.get(sourceIdOf(incoming)) : null
      const sameDepthScroll = (
        String(incoming?.action_type || '').trim().toLowerCase() === 'scroll'
        && parent
        && numberValue(parent.depth) !== null
        && numberValue(parent.depth) === numberValue(state.depth)
      )
      if (String(state?.stable_status || '').toUpperCase() === 'VIEWPORT' || sameDepthScroll) {
        viewportStateIds.add(stateId)
      }
    })

    const nonNavigationActionsOf = stateId => (
      (outgoingByState.get(stateId) || []).filter(transition => {
        const canonicalChildId = canonicalChildByTransition.get(transitionIdOf(transition))
        if (canonicalChildId !== undefined && stateMap.has(canonicalChildId)) return false
        const targetId = targetIdOf(transition)
        return !(isMeaningfulTarget(transition) && targetId !== null && stateMap.has(targetId))
      })
    )

    const rendered = new Set()
    const buildViewport = (stateId, pageLevel, transition) => {
      const state = stateMap.get(stateId)
      return makeNode({
        id: `state-${stateId}`,
        name: stateLabel(state, { level: pageLevel, viewport: true }),
        kind: 'state',
        // Keep the logical page level unchanged. The node is a terminal
        // auxiliary leaf; its discovered pages are promoted beside it.
        level: pageLevel,
        payload: {
          ...state,
          tree_role: 'viewport',
          viewport_auxiliary: true,
          non_navigation_actions: nonNavigationActionsOf(stateId),
        },
        children: [],
        incomingTransition: transition,
        lineStyle: pageTreeLineStyle(),
      })
    }

    const flattenedViewportChildren = (
      viewportId,
      pageLevel,
      ancestors,
      viewportAncestors,
    ) => {
      const children = []
      const nextViewportAncestors = new Set(viewportAncestors)
      nextViewportAncestors.add(viewportId)

      ;(outgoingByState.get(viewportId) || []).sort(bySequence).forEach(transition => {
        const transitionId = transitionIdOf(transition)
        const targetId = targetIdOf(transition)
        const canonicalChildId = canonicalChildByTransition.get(transitionId)

        if (canonicalChildId !== undefined && stateMap.has(canonicalChildId)) {
          if (viewportStateIds.has(canonicalChildId)) {
            if (
              nextViewportAncestors.has(canonicalChildId)
              || ancestors.has(canonicalChildId)
              || rendered.has(canonicalChildId)
            ) {
              children.push(referenceNode(canonicalChildId, pageLevel + 1, transition, stateMap))
              return
            }
            rendered.add(canonicalChildId)
            children.push(buildViewport(canonicalChildId, pageLevel, transition))
            children.push(...flattenedViewportChildren(
              canonicalChildId,
              pageLevel,
              ancestors,
              nextViewportAncestors,
            ))
            return
          }
          children.push(buildState(
            canonicalChildId,
            pageLevel + 1,
            ancestors,
            { incomingTransition: transition },
          ))
          return
        }

        if (isMeaningfulTarget(transition) && targetId !== null && stateMap.has(targetId)) {
          children.push(referenceNode(targetId, pageLevel + 1, transition, stateMap))
        }
      })

      return children
    }

    const buildState = (stateId, level, ancestors = new Set(), options = {}) => {
      const state = stateMap.get(stateId)
      if (!state) return referenceNode(stateId, level, options.incomingTransition, stateMap)
      if (ancestors.has(stateId) || rendered.has(stateId)) {
        return referenceNode(stateId, level, options.incomingTransition, stateMap)
      }
      rendered.add(stateId)
      const nextAncestors = new Set(ancestors)
      nextAncestors.add(stateId)

      const children = []
      ;(outgoingByState.get(stateId) || []).sort(bySequence).forEach(transition => {
        const transitionId = transitionIdOf(transition)
        const targetId = targetIdOf(transition)
        const canonicalChildId = canonicalChildByTransition.get(transitionId)
        if (canonicalChildId !== undefined && stateMap.has(canonicalChildId)) {
          if (viewportStateIds.has(canonicalChildId)) {
            if (nextAncestors.has(canonicalChildId) || rendered.has(canonicalChildId)) {
              children.push(referenceNode(canonicalChildId, level + 1, transition, stateMap))
              return
            }
            rendered.add(canonicalChildId)
            children.push(buildViewport(canonicalChildId, level, transition))
            children.push(...flattenedViewportChildren(
              canonicalChildId,
              level,
              nextAncestors,
              new Set(),
            ))
            return
          }
          children.push(buildState(canonicalChildId, level + 1, nextAncestors, { incomingTransition: transition }))
          return
        }
        if (isMeaningfulTarget(transition) && targetId !== null && stateMap.has(targetId)) {
          children.push(referenceNode(targetId, level + 1, transition, stateMap))
        }
      })

      return makeNode({
        id: `state-${stateId}`,
        name: stateLabel(state, {
          branchKey,
          level,
          root: Boolean(options.root),
          orphan: Boolean(options.orphan),
          incomingTransition: options.incomingTransition || null,
        }),
        kind: 'state',
        level,
        payload: {
          ...state,
          orphan: Boolean(options.orphan),
          tree_role: options.root ? 'home' : options.orphan ? 'orphan' : 'page',
          non_navigation_actions: nonNavigationActionsOf(stateId),
        },
        children,
        incomingTransition: options.incomingTransition || null,
        // A root has no incoming edge when rendered as its own series. When
        // filtered data falls back to the shared virtual root, this same style
        // keeps that first relationship visible and consistent with all other
        // page levels.
        lineStyle: pageTreeLineStyle(),
      })
    }

    const minimumDepth = Math.min(...branchStates.map(state => numberValue(state.depth) ?? 0), 0)
    const rootStates = branchStates.filter(state => {
      if (canonicalByChild.has(stateIdOf(state))) return false
      const hasParentField = Object.prototype.hasOwnProperty.call(state || {}, 'parent_state_id')
      if (hasParentField) return numberValue(state.parent_state_id) === null
      return (numberValue(state.depth) ?? 0) === minimumDepth
    })
    rootStates.forEach(state => pageRoots.push(buildState(stateIdOf(state), 0, new Set(), { root: true })))
    branchStates.forEach(state => {
      const stateId = stateIdOf(state)
      if (!rendered.has(stateId)) pageRoots.push(buildState(stateId, 0, new Set(), { orphan: true }))
    })
  })

  return makeNode({
    id: `page-tree-${runId}`,
    kind: 'virtual-root',
    level: -1,
    payload: { run_id: runId },
    children: pageRoots,
  })
}

export const collectExpandableMindMapNodes = root => {
  const result = []
  const visit = node => {
    if (node?.children?.length) result.push(node)
    node?.children?.forEach(visit)
  }
  visit(root)
  return result
}

export const applyMindMapCollapseState = (root, preferences = new Map()) => {
  const visit = node => {
    const childResults = (node.children || []).map(visit)
    const children = childResults.map(item => item.node)
    const preferred = preferences instanceof Map ? preferences.get(node.id) : preferences?.[node.id]
    const folded = children.length
      ? preferred ?? (node.kind === 'state' && node.level >= 1)
      : false
    const hiddenPageCount = childResults.reduce((total, item) => total + item.pageCount, 0)
    const hiddenItemCount = childResults.reduce((total, item) => total + item.itemCount, 0)
    const visibleChildren = folded
      ? [{
          id: `collapsed-branch-${node.id}`,
          name: hiddenPageCount
            ? `已收起 ${hiddenPageCount} 个页面`
            : `已收起 ${hiddenItemCount} 条关联`,
          kind: 'collapse-placeholder',
          level: (numberValue(node.level) ?? 0) + 1,
          payload: {
            owner_id: node.id,
            child_count: children.length,
            hidden_page_count: hiddenPageCount,
            hidden_item_count: hiddenItemCount,
          },
          children: [],
          symbol: 'circle',
          symbolSize: 6,
          itemStyle: {
            color: PAGE_TREE_LINE_STYLE.color,
            borderColor: PAGE_TREE_LINE_STYLE.color,
          },
          lineStyle: pageTreeLineStyle(),
          label: {
            show: true,
            color: '#909399',
            fontSize: 10,
          },
        }]
      : children
    const pageWeight = node.kind === 'state'
      ? Math.max(1, positiveNumber(node.payload?.aggregated_count) || 1)
      : 0
    return {
      node: {
        ...node,
        children: visibleChildren,
        expandable: children.length > 0,
        folded,
      },
      pageCount: pageWeight + hiddenPageCount,
      itemCount: 1 + hiddenItemCount,
    }
  }
  return visit(root).node
}
