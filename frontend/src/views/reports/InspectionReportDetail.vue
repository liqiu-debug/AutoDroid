<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  Aim,
  ArrowLeft,
  Download,
  Expand,
  Fold,
  FullScreen,
  House,
  InfoFilled,
  Refresh,
  Search,
  Select,
  VideoPause,
  VideoPlay,
  ZoomIn,
  ZoomOut,
} from '@element-plus/icons-vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { TreeChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'
import dayjs from 'dayjs'
import api from '@/api'
import InspectionLivePanel from '@/components/InspectionLivePanel.vue'
import {
  actionTargetLabel,
  aggregateInspectionMindMap,
  applyMindMapCollapseState,
  assignInspectionDisplayLabels,
  boundedMindMapExportSize,
  buildInspectionMindMap,
  centerMindMapViewport,
  collectExpandableMindMapNodes,
  fitMindMapViewport,
  focusedMindMapScale,
  inspectionMindMapInitialFocusId,
  inspectionMindMapNodeNeedsAttention,
  inspectionMindMapNodePositions,
  inspectionStateDisplayLabel,
  inspectionThumbnailAssetRequest,
  inspectionThumbnailSymbolSize,
  mindMapScrollOptions,
  PAGE_TREE_LINE_STYLE,
  zoomMindMapViewport,
} from '@/utils/inspectionMindMap'
import { runStatusTagType } from '@/utils/statusMeta'
import { inspectionRunStatusLabel } from '@/utils/inspectionRunPresentation'
import {
  inspectionActionStatus,
  inspectionActionStatusMeta,
  inspectionAssetAvailabilityLabel,
  inspectionBoundaryEvidenceLabel,
  inspectionCaptureKindLabel,
  inspectionCoverageItemReason,
  inspectionCoverageItemStatusMeta,
  inspectionCoverageVerdictLabel,
  inspectionEvidenceQualityLabel,
  inspectionExecutionDispositionLabel,
  inspectionObservationOrdinal,
  inspectionPageDisplayName,
  inspectionPhaseLabel,
  inspectionPageRoleLabel,
  inspectionReachabilityEvidence,
  inspectionReachabilityLabel,
  inspectionReplayEligibility,
  inspectionReplayEligibilityLabel,
  inspectionReportSummary,
  inspectionTerminalReviewState,
  inspectionTerminalOutcomeLabel,
  INSPECTION_ACTION_STATUS_META,
  INSPECTION_LOCATOR_FAILURE_STATUSES,
} from '@/utils/inspectionPresentation'

use([CanvasRenderer, TreeChart, TooltipComponent])

const route = useRoute()
const router = useRouter()
const runId = Number(route.params.id)
const run = ref(null)
const graph = ref({ schema_version: 6, hierarchy_version: 1, nodes: [], links: [], tree: {}, stats: {}, summary: {} })
const loading = ref(false)
const activeTab = ref('live')
const initialTabResolved = ref(false)
const branchFilter = ref('')
const stateFilter = ref('all')
const treeMode = ref('key')
const chartRef = ref(null)
const mindMapViewportRef = ref(null)
const mindMapScale = ref(1)
const mindMapSearchId = ref('')
const mindMapHasInitialFocus = ref(false)
const isMindMapDragging = ref(false)
const objectUrls = new Set()
const thumbnailUrls = shallowRef({})
const thumbnailDimensions = shallowRef({})
const thumbnailSourceKeys = shallowRef({})
const thumbnailLoading = new Set()
const thumbnailCache = new Map()
const thumbnailRetryTimers = new Map()
const selectedNode = ref(null)
const selectedAction = ref(null)
const selectedIncomingAction = ref(null)
const liveStateId = ref(null)
const drawerVisible = ref(false)
const detailImageUrl = ref('')
const xmlText = ref('')
const actionMap = ref({ actions: [], screen_size: null })
const detailLoading = ref(false)
const observations = ref([])
const observationsTotal = ref(0)
const observationPage = ref(1)
const observationPageSize = 10
const observationLoading = ref(false)
const representativeSaving = ref(false)
const selectedObservation = ref(null)
const assetNotice = ref('')
const selectedIds = ref([])
const families = ref([])
const collapsePreferences = ref(new Map())
const selectedMindMapNodeId = ref('')
const diagnosticPanels = ref([])
const detailImageSize = ref({ width: 0, height: 0 })
let pollTimer = null
let detailRequestId = 0
let mindMapPointer = null
let componentUnmounted = false

const representativeObservationId = computed(() => Number(selectedNode.value?.representative_observation_id) || null)
const activeEvidence = computed(() => selectedObservation.value || selectedNode.value)
const hasXmlEvidence = computed(() => Boolean(
  activeEvidence.value?.xml_asset_id
  || activeEvidence.value?.representative_xml_asset_id
  || activeEvidence.value?.xml_path
  || selectedNode.value?.xml_asset_id
  || selectedNode.value?.xml_path,
))

const fetchStoredOrLegacyAsset = async (assetId, path, responseType = 'blob') => {
  if (assetId) {
    try {
      return await api.getAsset(assetId, responseType)
    } catch (assetError) {
      if (!path || ![404, 410].includes(Number(assetError.response?.status))) throw assetError
      try {
        return await api.getInspectionAsset(runId, path, responseType)
      } catch (legacyError) {
        if (Number(assetError.response?.status) === 410 && Number(legacyError.response?.status) === 404) {
          throw assetError
        }
        throw legacyError
      }
    }
  }
  if (path) return api.getInspectionAsset(runId, path, responseType)
  return { data: null }
}

const terminal = status => ['PASS', 'WARNING', 'FAIL', 'ERROR', 'ABORTED', 'CANCELLED'].includes(String(status || '').toUpperCase())
const graphSchemaVersion = computed(() => Number(graph.value?.schema_version || 1))
const runPhase = computed(() => inspectionPhaseLabel(
  run.value?.current_phase || run.value?.phase || run.value?.current_stage
    || graph.value?.current_phase || graph.value?.phase || graph.value?.current_stage,
))
const frontierCounts = computed(() => {
  const source = { ...run.value, ...run.value?.frontier, ...graph.value?.stats, ...graph.value?.frontier }
  const value = (...keys) => {
    const found = keys.map(key => source[key]).find(item => item !== undefined && item !== null)
    return found === undefined ? null : Number(found)
  }
  return {
    queued: value('queued_count', 'queued_states', 'queued'),
    deferred: value('deferred_count', 'deferred_states', 'deferred'),
    pending: value('pending_action_count', 'pending_actions', 'pending'),
  }
})
const familyById = computed(() => new Map(families.value.map(item => [Number(item.id || item.family_id), item])))
const familyForNode = node => {
  const familyId = node?.exploration_family_id || node?.family_id
  if (!familyId) return null
  return familyById.value.get(Number(familyId)) || { id: familyId, name: node?.exploration_family_name }
}
const explorationModeLabel = value => ({
  FULL: '代表页完整遍历',
  DELTA_ONLY: '增量遍历',
  INDEPENDENT: '独立遍历',
}[String(value || '').toUpperCase()] || value || '-')
const expansionStatusLabel = value => ({
  DISCOVERED: '已发现',
  QUEUED: '待展开',
  EXPANDING: '展开中',
  DEFERRED: '延迟恢复',
  EXPANDED: '已展开',
  BUDGET_SKIPPED: '预算跳过',
  SCOPE_SKIPPED: '超出单页范围',
  COVERED_BY_SURFACE: '同面已覆盖',
  ABORTED: '已中止',
}[String(value || '').toUpperCase()] || value || '-')
const presentedNodes = computed(() => {
  const linksBySource = new Map()
  ;(graph.value.links || []).forEach(link => {
    const sourceId = Number(link.source ?? link.from_state_id)
    if (!linksBySource.has(sourceId)) linksBySource.set(sourceId, [])
    linksBySource.get(sourceId).push(link)
  })
  return assignInspectionDisplayLabels(graph.value.nodes || []).map(node => {
    const outgoing = linksBySource.get(Number(node.state_id)) || []
    const terminalBoundaries = Array.isArray(node.terminal_boundaries) && node.terminal_boundaries.length
      ? node.terminal_boundaries
      : outgoing.filter(link => (
        ['BLOCKED', 'SAFETY_BLOCKED'].includes(String(link.status || link.failure_type || '').toUpperCase())
      )).map(link => ({
        terminal_outcome: 'SAFETY_BLOCKED',
        risk_type: link.risk_type,
        action_type: link.action_type,
        action_label: actionTargetLabel(link),
        transition_id: link.id,
      }))
    return {
      ...node,
      terminal_boundaries: terminalBoundaries,
      reachability_evidence: inspectionReachabilityEvidence(node),
      replay_eligibility: inspectionReplayEligibility({ ...node, terminal_boundaries: terminalBoundaries }, outgoing),
    }
  })
})
const nodeByStateId = computed(() => new Map(presentedNodes.value.map(item => [Number(item.state_id), item])))
const pageLabelForStateId = stateId => inspectionStateDisplayLabel(nodeByStateId.value.get(Number(stateId)))
const pageTitleForNode = node => node?.page_title
  || node?.title
  || (String(node?.page_role || '').toUpperCase() === 'HOME'
    ? `${node?.branch_key === 'authenticated' ? '已登录' : node?.branch_key === 'guest' ? '未登录' : ''}首页`
    : inspectionPageRoleLabel(node?.page_role || node?.template_role))
const branchDisplayLabel = branch => branch === 'guest' ? '未登录' : branch === 'authenticated' ? '已登录' : branch || '-'
const reportSummary = computed(() => inspectionReportSummary({
  graph: graph.value,
  run: run.value || {},
  nodes: presentedNodes.value,
}))
const businessCoverage = computed(() => reportSummary.value.business || {})
const coverageBranches = computed(() => businessCoverage.value.branches || [])
const coverageBlindSpots = computed(() => businessCoverage.value.blindSpots || [])
const surfaceCoverage = computed(() => businessCoverage.value.surface || {})
const surfaceMetricClass = computed(() => (
  surfaceCoverage.value.verdict === 'COMPLETE' ? 'success' : 'danger'
))
// Surfaces the map knows about that no run has ever exercised, plus the ones
// this run discovered but never expanded.  Listing them by name is the point:
// a report that can say what it did not check is the one worth trusting.
const surfaceGapCount = computed(() => (
  (surfaceCoverage.value.neverCovered || []).length
  + (surfaceCoverage.value.stale || []).length
))
const coverageMetricClass = computed(() => (
  businessCoverage.value.selectedScopeVerdict === 'COMPLETE' ? 'success'
    : businessCoverage.value.selectedScopeVerdict === 'INCONCLUSIVE' ? 'warning' : 'danger'
))
const evidenceQualityClass = computed(() => ({
  HIGH: 'success',
  MEDIUM: 'warning',
  LOW: 'danger',
}[businessCoverage.value.evidenceQuality] || 'warning'))
const evidenceStateNodes = item => (item?.evidence_state_ids || [])
  .map(id => nodeByStateId.value.get(Number(id)))
  .filter(Boolean)

// Single-page scoped branches: explicit denominator (every enumerated action
// on the entry surface) plus the reached-but-unconfigured surface work list.
const scopeCoverageBranches = computed(() => graph.value?.scope_coverage?.branches || [])
const scopeSkipRows = branch => Object.entries(branch?.skipped_by_status || {})
  .map(([status, count]) => ({
    status,
    label: INSPECTION_ACTION_STATUS_META[String(status || '').toUpperCase()]?.label || status,
    count,
  }))
  .sort((a, b) => b.count - a.count)
const scopeSurfaceStateNodes = item => (item?.state_ids || [])
  .map(id => nodeByStateId.value.get(Number(id)))
  .filter(Boolean)
const transitionById = computed(() => new Map(
  (graph.value.links || []).map(item => [Number(item.id), item]),
))
const evidenceTransitions = item => (item?.evidence_transition_ids || [])
  .map(id => transitionById.value.get(Number(id)))
  .filter(Boolean)
const evidenceTransitionLabel = transition => (
  Number.isFinite(Number(transition?.sequence)) ? `#${Number(transition.sequence)}` : '动作证据'
)
const openCoverageState = node => {
  if (node) void openNode(node)
}
const attentionSummaryText = computed(() => {
  const issues = reportSummary.value.issues || {}
  const parts = [
    ['应用故障', issues.application],
    ['设备问题', issues.infrastructure],
    ['巡检受阻', issues.automation],
  ].filter(([, count]) => Number(count) > 0)
    .map(([label, count]) => `${label} ${count}`)
  return parts.length ? parts.join(' · ') : '没有需要人工处理的问题'
})
const replaySourceEligible = computed(() => (
  run.value?.replay_source_eligible !== false
  && graph.value?.replay_source_eligible !== false
))
const replayEvidenceAvailable = computed(() => {
  const explicit = run.value?.replay_evidence_available
    ?? graph.value?.replay_evidence_available
  return explicit === undefined ? replaySourceEligible.value : Boolean(explicit)
})
const canOpenInstalledReplay = computed(() => {
  if (!run.value || !terminal(run.value.status) || !replayEvidenceAvailable.value) return false
  return !['CANCELLED', 'ABORTED'].includes(String(run.value.terminal_outcome || run.value.status || '').toUpperCase())
})
const replaySourceReason = computed(() => (
  run.value?.replay_source_reason
  || graph.value?.replay_source_reason
  || '任务没有形成可回放证据'
))
const currentReportStateId = computed(() => Number(
  liveStateId.value
    || run.value?.last_active_state_id
    || run.value?.current_state_id
    || graph.value?.last_active_state_id
    || graph.value?.current_state_id,
) || null)
const currentReportPage = computed(() => {
  const node = nodeByStateId.value.get(currentReportStateId.value)
  return node ? inspectionPageDisplayName(node, inspectionStateDisplayLabel(node)) : '等待页面数据'
})
const currentReportAction = computed(() => String(
  run.value?.current_action_label
    || run.value?.current_action?.label
    || graph.value?.current_action_label
    || '等待下一步',
))
const diagnostics = computed(() => {
  const stats = graph.value.stats || {}
  const detail = graph.value.diagnostics || {}
  const contract = stats.coverage_contracts || {}
  const locatorMethodLabels = {
    description: '描述',
    text: '文本',
    xpath: 'XPath',
    coordinate: '坐标',
    'semantic-bounds': '语义范围',
    'scroll:up:coordinate': '坐标上滑',
    'scroll:down:coordinate': '坐标下滑',
    back: '返回',
  }
  const locatorMethods = Object.entries(detail.locator_methods || stats.locator_methods || {})
    .map(([key, value]) => `${locatorMethodLabels[key] || key} ${value}`)
    .join(' / ') || '-'
  const risks = Object.entries(detail.risks || stats.risks || {}).map(([key, value]) => `${key} ${value}`).join(' / ') || '-'
  const reachability = Object.entries(detail.reachability || {})
    .map(([key, value]) => `${inspectionReachabilityLabel(key)} ${value}`)
    .join(' / ') || '-'
  const replayEligibility = Object.entries(detail.replay_eligibility || {})
    .map(([key, value]) => `${inspectionReplayEligibilityLabel(key)} ${value}`)
    .join(' / ') || '-'
  const frontier = detail.frontier || {}
  return [
    ['报告格式', `v${graphSchemaVersion.value}`],
    ['执行设备', run.value?.device_serial || '-'],
    ['已发现页面族展开率', reportSummary.value.summaryAvailable
      ? `${reportSummary.value.family.expanded}/${reportSummary.value.family.total} · ${Math.round(reportSummary.value.family.ratio * 1000) / 10}%`
      : '旧版报告缺少指标'],
    ['可回放路径', replaySourceEligible.value
      ? `${reportSummary.value.replay.total}（完整 ${reportSummary.value.replay.full} / 安全前缀 ${reportSummary.value.replay.safePrefix}）`
      : replaySourceReason.value],
    ['需关注问题', `${reportSummary.value.attention} · ${attentionSummaryText.value}`],
    ['设备动作', stats.actual_device_actions ?? stats.transitions ?? 0],
    ['安全拦截', stats.blocked || 0],
    ['终态未执行', stats.terminal_unexecuted || 0],
    ['覆盖契约', `临时 ${contract.provisional || 0} / 已验证 ${contract.verified || 0} / 冲突 ${contract.conflict || 0}`],
    ['契约复用 / 采样跳过', `${stats.covered_by_contract || 0} / ${stats.sampled_out || 0}`],
    ['导航复用 / 视觉入口', `${stats.navigation_reused || 0} / ${stats.visual_entries || 0}`],
    ['定位方式', locatorMethods],
    ['坐标过期 / 不安全', `${stats.coordinate_stale || 0} / ${stats.coordinate_unsafe || 0}`],
    ['定位缺失 / 歧义', `${stats.locator_not_found || 0} / ${stats.locator_ambiguous ?? stats.ambiguous ?? 0}`],
    ['父页恢复失败', stats.parent_recovery_failed || 0],
    ['路径偏离', stats.path_diverged || 0],
    ['动作异常', stats.action_errors || 0],
    ['采集记录', stats.observations || 0],
    ['循环分量', stats.cycles || 0],
    ['风险动作', risks],
    ['到达证据', reachability],
    ['回放能力', replayEligibility],
    ['队列状态', `排队 ${frontier.queued ?? frontierCounts.value.queued ?? 0} / 延迟 ${frontier.deferred ?? frontierCounts.value.deferred ?? 0} / 待执行 ${frontier.pending ?? frontierCounts.value.pending ?? 0}`],
  ]
})
const branchOptions = computed(() => [...new Set(presentedNodes.value.map(item => item.branch_key))])
const filteredNodes = computed(() => presentedNodes.value.filter(item => {
  if (branchFilter.value && item.branch_key !== branchFilter.value) return false
  if (stateFilter.value === 'replayable' && !['FULL', 'SAFE_PREFIX'].includes(inspectionReplayEligibility(item))) return false
  if (stateFilter.value === 'attention' && !inspectionMindMapNodeNeedsAttention(item)) return false
  return true
}))
const filteredNodeIds = computed(() => new Set(filteredNodes.value.map(item => String(item.id))))
const filteredLinks = computed(() => (graph.value.links || []).filter(item => (
  filteredNodeIds.value.has(String(item.source))
)))

const mindMapBase = computed(() => buildInspectionMindMap({
  runId,
  runName: run.value?.name,
  hierarchyVersion: graph.value?.hierarchy_version,
  nodes: filteredNodes.value,
  links: filteredLinks.value,
}))

const modeMindMapBase = computed(() => aggregateInspectionMindMap(mindMapBase.value, treeMode.value))

const homeMindMapStateId = computed(() => {
  let stateId = null
  const visit = node => {
    if (!node || stateId) return
    if (node.kind === 'state' && node.payload?.tree_role === 'home') {
      stateId = Number(node.payload?.state_id) || null
      return
    }
    ;(node.children || []).forEach(visit)
  }
  visit(modeMindMapBase.value)
  return stateId
})

const initialMindMapFocusId = computed(() => inspectionMindMapInitialFocusId({
  status: run.value?.status,
  terminalOutcome: run.value?.terminal_outcome || graph.value?.terminal_outcome,
  lastActiveStateId: run.value?.last_active_state_id || graph.value?.last_active_state_id,
  currentStateId: run.value?.current_state_id || graph.value?.current_state_id || liveStateId.value,
  homeStateId: homeMindMapStateId.value,
}))

const decoratePageThumbnails = node => {
  const source = node.kind === 'state' ? inspectionThumbnailAssetRequest(node.payload, runId) : null
  const thumbnail = node.kind === 'state'
    && node.payload?.tree_role !== 'viewport'
    && thumbnailSourceKeys.value[node.payload?.state_id] === source?.key
    ? thumbnailUrls.value[node.payload?.state_id]
    : null
  return {
    ...node,
    ...(thumbnail ? {
      symbol: `image://${thumbnail}`,
      symbolSize: inspectionThumbnailSymbolSize(
        node.payload,
        thumbnailDimensions.value[node.payload?.state_id],
      ),
    } : {}),
    ...(node.id === selectedMindMapNodeId.value ? {
      itemStyle: { ...(node.itemStyle || {}), borderColor: '#409eff', borderWidth: 3 },
    } : {}),
    children: (node.children || []).map(decoratePageThumbnails),
  }
}

const mindMapTree = computed(() => decoratePageThumbnails(
  applyMindMapCollapseState(modeMindMapBase.value, collapsePreferences.value),
))

const mindMapLayoutMetrics = computed(() => {
  let leaves = 0
  let maxDepth = 0
  const visit = (node, depth = 0) => {
    if (!node) return
    maxDepth = Math.max(maxDepth, depth)
    const children = node.children || []
    if (!children.length) leaves += 1
    children.forEach(child => visit(child, depth + 1))
  }
  visit(mindMapTree.value)
  return {
    leaves: Math.max(1, leaves),
    maxDepth: Math.max(1, maxDepth),
  }
})

// ECharts distributes tree leaves over the series' actual canvas. A fixed
// short canvas makes a large report unreadable even when thumbnails are kept
// proportional, so the surrounding viewport scrolls a bounded report-sized
// canvas instead.
const mindMapCanvasWidth = computed(() => Math.max(
  1400,
  Math.min(7200, 420 + mindMapLayoutMetrics.value.maxDepth * 260),
))
const mindMapCanvasHeight = computed(() => Math.max(
  720,
  Math.min(16000, mindMapLayoutMetrics.value.leaves * 124),
))
const scaledMindMapWidth = computed(() => Math.max(1, mindMapCanvasWidth.value * mindMapScale.value))
const scaledMindMapHeight = computed(() => Math.max(1, mindMapCanvasHeight.value * mindMapScale.value))
const mindMapSearchOptions = computed(() => filteredNodes.value.map(node => ({
  id: `state-${node.state_id}`,
  label: inspectionPageDisplayName(node, inspectionStateDisplayLabel(node)),
})))

const escapeHtml = value => String(value ?? '')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')
  .replace(/'/g, '&#039;')

const tooltipFormatter = params => {
  const item = params.data || {}
  const payload = item.payload || {}
  if (item.kind === 'state') {
    return [
      `<strong>${escapeHtml(item.name)}</strong>`,
      `${escapeHtml(branchDisplayLabel(payload.branch_key))} · ${escapeHtml(inspectionPageRoleLabel(payload.page_role || payload.template_role))}`,
      `${escapeHtml(inspectionReachabilityLabel(payload.reachability_evidence))} · ${escapeHtml(inspectionReplayEligibilityLabel(payload.replay_eligibility))}`,
      `页面动作：${escapeHtml(payload.non_navigation_actions?.length || 0)}`,
      item.incomingLabel ? `进入动作：${escapeHtml(item.incomingLabel)}` : '',
    ].filter(Boolean).join('<br/>')
  }
  if (item.kind === 'action-leaf') {
    return [
      `<strong>${escapeHtml(item.name)}</strong>`,
      `${escapeHtml(payload.status || '-')} · ${escapeHtml(payload.duration_ms ?? 0)}ms`,
      escapeHtml(payload.reason || payload.error_message || ''),
    ].filter(Boolean).join('<br/>')
  }
  if (item.kind === 'reference') {
    return `${escapeHtml(item.name)}<br/>${escapeHtml(item.payload?.transition?.status || '-')}`
  }
  if (item.kind === 'collapse-placeholder') {
    return escapeHtml(item.name || '已收起分支')
  }
  return escapeHtml(item.name || '-')
}

const MIND_MAP_SERIES_ID = 'inspection-page-tree'
const MIND_MAP_INIT_OPTIONS = Object.freeze({ devicePixelRatio: 1 })
const mindMapOption = computed(() => ({
  tooltip: {
    trigger: 'item',
    formatter: tooltipFormatter,
    confine: true,
  },
  series: [{
    id: MIND_MAP_SERIES_ID,
    type: 'tree',
    data: [mindMapTree.value],
    layout: 'orthogonal',
    orient: 'LR',
    // Panning and zooming are owned by the scroll viewport below. Keeping
    // ECharts roam disabled avoids two independent transforms drifting apart.
    roam: false,
    // symbolSize already preserves each thumbnail's natural ratio. Enabling
    // ECharts' image containment here would apply that ratio a second time.
    symbolKeepAspect: false,
    expandAndCollapse: false,
    initialTreeDepth: -1,
    edgeShape: 'polyline',
    edgeForkPosition: '45%',
    top: 72,
    left: 28,
    right: 150,
    bottom: 36,
    animationDuration: filteredNodes.value.length < 150 ? 300 : 0,
    animationDurationUpdate: filteredNodes.value.length < 150 ? 300 : 0,
    lineStyle: { ...PAGE_TREE_LINE_STYLE },
    label: {
      position: 'right',
      verticalAlign: 'middle',
      align: 'left',
      distance: 9,
      color: '#303133',
      fontSize: 12,
      formatter: params => {
        const item = params.data || {}
        if (item.kind !== 'state') return item.name || ''
        const parts = String(item.name || '').split(' · ')
        const displayIndex = parts.findIndex(part => /^P\d+$/.test(part))
        const pageName = displayIndex > 0 ? parts.slice(0, displayIndex).join(' · ') : parts[0]
        const suffix = displayIndex > 0 ? parts.slice(displayIndex).join(' · ') : parts.slice(1).join(' · ')
        return suffix
          ? `{page|${pageName}}\n{id|${suffix}}`
          : `{page|${pageName}}`
      },
      rich: {
        page: { color: '#303133', fontSize: 12, fontWeight: 600, lineHeight: 18 },
        id: { color: '#909399', fontSize: 10, lineHeight: 15 },
      },
    },
    leaves: {
      label: { position: 'right', verticalAlign: 'middle', align: 'left' },
    },
    emphasis: { focus: 'none', itemStyle: { borderWidth: 3 } },
  }],
}))

const stableStates = computed(() => presentedNodes.value.filter(item => (
  item.reachability_evidence === 'VERIFIED_TWICE'
  && item.locator_quality !== 'COORDINATE_ONLY'
)))
const terminalReviewState = computed(() => {
  const states = graph.value.nodes || []
  return inspectionTerminalReviewState(states, {
    liveStateId: liveStateId.value,
    last_active_state_id: run.value?.last_active_state_id || graph.value?.last_active_state_id,
    current_state_id: run.value?.current_state_id || graph.value?.current_state_id,
    last_state_id: run.value?.last_state_id || graph.value?.last_state_id,
    last_observation_id: run.value?.last_observation_id || graph.value?.last_observation_id,
  })
})

const thumbnailRequestIsRetryable = error => {
  const status = Number(error?.response?.status || 0)
  return !status || status === 401 || status === 408 || status === 429 || status >= 500
}

const fetchThumbnailRecord = async request => {
  let lastError
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      if (componentUnmounted) throw new Error('页面已关闭')
      const response = await fetchStoredOrLegacyAsset(request.assetId, request.path, 'blob')
      if (!(response.data instanceof Blob) || response.data.size === 0) throw new Error('缩略图资产为空')
      if (componentUnmounted) throw new Error('页面已关闭')
      const url = URL.createObjectURL(response.data)
      objectUrls.add(url)
      const dimensions = await new Promise(resolve => {
        const image = new Image()
        image.onload = () => resolve({ width: image.naturalWidth, height: image.naturalHeight })
        image.onerror = () => resolve(null)
        image.src = url
      })
      return { url, dimensions }
    } catch (error) {
      lastError = error
      if (componentUnmounted || attempt >= 2 || !thumbnailRequestIsRetryable(error)) break
      await new Promise(resolve => {
        const retryKey = `${request.key}:${attempt}`
        const timer = window.setTimeout(() => {
          thumbnailRetryTimers.delete(retryKey)
          resolve()
        }, 250 * (attempt + 1))
        thumbnailRetryTimers.set(retryKey, timer)
      })
    }
  }
  throw lastError
}

const hydrateThumbnail = async node => {
  const stateId = Number(node?.state_id)
  const request = inspectionThumbnailAssetRequest(node, runId)
  if (!stateId || !request) return
  if (thumbnailSourceKeys.value[stateId] === request.key && thumbnailUrls.value[stateId]) return

  let cached = thumbnailCache.get(request.key)
  if (!cached) {
    const promise = fetchThumbnailRecord(request)
    cached = { promise }
    thumbnailCache.set(request.key, cached)
    thumbnailLoading.add(request.key)
    promise.then(record => {
      thumbnailCache.set(request.key, record)
    }).catch(() => {
      thumbnailCache.delete(request.key)
    }).finally(() => {
      thumbnailLoading.delete(request.key)
    })
  }

  try {
    const record = cached.promise ? await cached.promise : cached
    if (componentUnmounted || inspectionThumbnailAssetRequest(node, runId)?.key !== request.key) return
    thumbnailSourceKeys.value = { ...thumbnailSourceKeys.value, [stateId]: request.key }
    thumbnailUrls.value = { ...thumbnailUrls.value, [stateId]: record.url }
    if (record.dimensions?.width && record.dimensions?.height) {
      thumbnailDimensions.value = { ...thumbnailDimensions.value, [stateId]: record.dimensions }
    }
  } catch {
    // A missing thumbnail must not prevent the page hierarchy from rendering.
  }
}

const hydrateInitialPageThumbnails = nodes => {
  const roots = nodes.filter(item => (
    inspectionThumbnailAssetRequest(item, runId)
    && (item.parent_state_id === null || Number(item.depth || 0) === 0)
    && String(item.stable_status || '').toUpperCase() !== 'VIEWPORT'
  ))
  const rootIds = new Set(roots.map(item => Number(item.state_id)))
  const firstVisibleLayer = nodes.filter(item => (
    inspectionThumbnailAssetRequest(item, runId)
    && rootIds.has(Number(item.parent_state_id))
    && String(item.stable_status || '').toUpperCase() !== 'VIEWPORT'
  ))
  // 首页默认展开，只加载首屏可见的首页和直接子页面，不预取整棵树。
  const visible = [...roots, ...firstVisibleLayer].slice(0, 32)
  visible.forEach(item => { void hydrateThumbnail(item) })
}

const releaseUnusedThumbnailCache = nodes => {
  const activeKeys = new Set(nodes.map(item => inspectionThumbnailAssetRequest(item, runId)?.key).filter(Boolean))
  thumbnailCache.forEach((record, key) => {
    if (activeKeys.has(key) || record?.promise || !record?.url) return
    URL.revokeObjectURL(record.url)
    objectUrls.delete(record.url)
    thumbnailCache.delete(key)
  })
}

const hydrateDirectChildThumbnails = node => {
  const findNode = (current, id) => {
    if (!current || !id) return null
    if (current.id === id) return current
    for (const child of current.children || []) {
      const match = findNode(child, id)
      if (match) return match
    }
    return null
  }
  const sourceNode = findNode(mindMapBase.value, node?.id) || node
  const children = (sourceNode?.children || [])
    .filter(item => (
      item.kind === 'state'
      && item.payload?.tree_role !== 'viewport'
      && inspectionThumbnailAssetRequest(item.payload, runId)
    ))
    .slice(0, 32)
  children.forEach(item => { void hydrateThumbnail(item.payload) })
}

const fetchData = async (quiet = false) => {
  if (!quiet) loading.value = true
  try {
    const [runResponse, graphResponse] = await Promise.all([
      api.getInspectionRun(runId),
      api.getInspectionGraph(runId),
    ])
    run.value = runResponse.data
    graph.value = graphResponse.data || { schema_version: 6, hierarchy_version: 1, nodes: [], links: [], tree: {}, stats: {}, summary: {} }
    releaseUnusedThumbnailCache(graph.value.nodes || [])
    selectedIds.value = (graph.value.nodes || []).filter(item => item.selected_for_regression).map(item => item.state_id)
    hydrateInitialPageThumbnails(graph.value.nodes || [])
    if (!initialTabResolved.value) {
      activeTab.value = terminal(runResponse.data?.status)
        ? ((runResponse.data?.coverage_assessment?.manifest || graphResponse.data?.coverage_assessment?.manifest)
          ? 'coverage'
          : 'page-tree')
        : 'live'
      initialTabResolved.value = true
    }
    if (graphSchemaVersion.value >= 4 && typeof api.getInspectionFamilies === 'function') {
      api.getInspectionFamilies(runId).then(response => {
        const data = response.data
        families.value = Array.isArray(data) ? data : (data?.items || data?.families || [])
      }).catch(() => { families.value = [] })
    } else {
      families.value = []
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || '加载巡检报告失败')
  } finally {
    loading.value = false
  }
}

const fetchNodeImage = async (node, requestId, observation = null) => {
  if (detailImageUrl.value) {
    URL.revokeObjectURL(detailImageUrl.value)
    objectUrls.delete(detailImageUrl.value)
    detailImageUrl.value = ''
  }
  assetNotice.value = ''
  const evidence = observation || node
  detailImageSize.value = {
    width: Number(evidence?.original_width || evidence?.image_width || node?.image_width || 0) || 0,
    height: Number(evidence?.original_height || evidence?.image_height || node?.image_height || 0) || 0,
  }
  const assetId = evidence?.screenshot_asset_id
    || evidence?.representative_screenshot_asset_id
    || node?.screenshot_asset_id
    || node?.representative_screenshot_asset_id
  const path = evidence?.screenshot_path || node?.screenshot_path
  if (!assetId && !path) {
    if (evidence?.metadata_only || evidence?.asset_status === 'CLEANED') assetNotice.value = '完整截图已按保留策略清理，仅保留元数据和哈希。'
    return
  }
  let response
  try {
    response = await fetchStoredOrLegacyAsset(assetId, path, 'blob')
  } catch (error) {
    if (error.response?.status === 410) {
      assetNotice.value = '完整截图已按保留策略清理，仅保留元数据和哈希。'
      return
    }
    throw error
  }
  const url = URL.createObjectURL(response.data)
  if (requestId !== detailRequestId) {
    URL.revokeObjectURL(url)
    return
  }
  detailImageUrl.value = url
  objectUrls.add(url)
  const image = new Image()
  image.onload = () => {
    if (requestId !== detailRequestId || !image.naturalWidth || !image.naturalHeight) return
    detailImageSize.value = { width: image.naturalWidth, height: image.naturalHeight }
  }
  image.src = url
}

const normalizeObservationPage = data => {
  if (Array.isArray(data)) return { items: data, total: data.length }
  const items = data?.items || data?.observations || data?.results || []
  return {
    items: Array.isArray(items) ? items : [],
    total: Number(data?.total ?? data?.count ?? items.length) || 0,
  }
}

const loadObservations = async (node, page, requestId, preferRepresentative = false, preferredObservationId = null) => {
  observations.value = []
  observationsTotal.value = 0
  observationPage.value = page
  selectedObservation.value = null
  if (!node?.state_id || typeof api.getInspectionObservations !== 'function') return null
  observationLoading.value = true
  try {
    const params = {
      page,
      page_size: observationPageSize,
    }
    if (preferredObservationId) params.observation_id = preferredObservationId
    const response = await api.getInspectionObservations(runId, node.state_id, params)
    if (requestId !== detailRequestId) return null
    const normalized = normalizeObservationPage(response.data)
    observations.value = normalized.items
    observationsTotal.value = normalized.total
    const representativeId = Number(node.representative_observation_id)
    const preferred = normalized.items.find(item => (
      Number(item.id) === Number(preferredObservationId)
      || (preferRepresentative && (Number(item.id) === representativeId || item.is_representative))
    ))
    selectedObservation.value = preferred || normalized.items[0] || null
    return selectedObservation.value
  } catch (error) {
    if (requestId === detailRequestId && error.response?.status !== 404) throw error
    return null
  } finally {
    if (requestId === detailRequestId) observationLoading.value = false
  }
}

const fetchActionMap = async (node, requestId) => {
  actionMap.value = { actions: [], screen_size: null }
  if (!node?.state_id || typeof api.getInspectionActionMap !== 'function') return
  try {
    const response = await api.getInspectionActionMap(runId, node.state_id)
    const data = response.data || {}
    const actions = Array.isArray(data) ? data : Array.isArray(data.actions) ? data.actions : []
    const first = actions[0] || {}
    if (requestId !== detailRequestId) return
    actionMap.value = {
      actions,
      screen_size: data.screen_size || data.source_screen_size || {
        width: data.screen_width || data.source_width || first.source_width || first.screen_width,
        height: data.screen_height || data.source_height || first.source_height || first.screen_height,
      },
    }
  } catch (error) {
    if (requestId === detailRequestId && error.response?.status !== 404) {
      ElMessage.warning(error.response?.data?.detail || '历史动作地图加载失败')
    }
  }
}

const openNode = async (node, incomingTransition = null, preferredObservationId = null) => {
  if (!node?.state_id) return
  selectedNode.value = node
  selectedAction.value = null
  selectedIncomingAction.value = incomingTransition
  drawerVisible.value = true
  xmlText.value = ''
  assetNotice.value = ''
  detailLoading.value = true
  const requestId = ++detailRequestId
  void hydrateThumbnail(node)
  try {
    const [observation] = await Promise.all([
      loadObservations(node, 1, requestId, true, preferredObservationId),
      fetchActionMap(node, requestId),
    ])
    if (requestId === detailRequestId) await fetchNodeImage(node, requestId, observation)
  } catch (error) {
    if (requestId === detailRequestId) {
      ElMessage.error(error.response?.data?.detail || error.message || '状态详情加载失败')
    }
  } finally {
    if (requestId === detailRequestId) detailLoading.value = false
  }
}

const selectObservation = async observation => {
  if (!selectedNode.value || !observation || Number(selectedObservation.value?.id) === Number(observation.id)) return
  selectedObservation.value = observation
  xmlText.value = ''
  detailLoading.value = true
  const requestId = ++detailRequestId
  try {
    await fetchNodeImage(selectedNode.value, requestId, observation)
  } catch (error) {
    if (requestId === detailRequestId) ElMessage.error(error.response?.data?.detail || error.message || '采集资产加载失败')
  } finally {
    if (requestId === detailRequestId) detailLoading.value = false
  }
}

const setRepresentativeObservation = async () => {
  const stateId = Number(selectedNode.value?.state_id)
  const observationId = Number(selectedObservation.value?.id)
  if (!stateId || !observationId || observationId === representativeObservationId.value) return
  representativeSaving.value = true
  try {
    await api.updateInspectionRepresentative(runId, stateId, observationId)
    observations.value = observations.value.map(item => ({
      ...item,
      is_representative: Number(item.id) === observationId,
    }))
    selectedObservation.value = observations.value.find(item => Number(item.id) === observationId) || selectedObservation.value
    graph.value = {
      ...graph.value,
      nodes: (graph.value.nodes || []).map(item => (
        Number(item.state_id) === stateId
          ? { ...item, representative_observation_id: observationId }
          : item
      )),
    }
    selectedNode.value = {
      ...selectedNode.value,
      representative_observation_id: observationId,
    }
    ElMessage.success(`已将第 ${observationOrdinal(selectedObservation.value)} 次采集设为代表样本`)
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || '代表样本更新失败')
  } finally {
    representativeSaving.value = false
  }
}

const changeObservationPage = async page => {
  if (!selectedNode.value) return
  detailLoading.value = true
  const requestId = ++detailRequestId
  try {
    const observation = await loadObservations(selectedNode.value, page, requestId)
    if (requestId === detailRequestId) await fetchNodeImage(selectedNode.value, requestId, observation)
  } catch (error) {
    if (requestId === detailRequestId) ElMessage.error(error.response?.data?.detail || error.message || '采集记录加载失败')
  } finally {
    if (requestId === detailRequestId) detailLoading.value = false
  }
}

const openAction = transition => {
  if (!transition) return
  detailRequestId += 1
  selectedAction.value = transition
  selectedNode.value = null
  selectedIncomingAction.value = null
  drawerVisible.value = true
  xmlText.value = ''
  observations.value = []
  observationsTotal.value = 0
  selectedObservation.value = null
  assetNotice.value = ''
  actionMap.value = { actions: [], screen_size: null }
  if (detailImageUrl.value) {
    URL.revokeObjectURL(detailImageUrl.value)
    objectUrls.delete(detailImageUrl.value)
    detailImageUrl.value = ''
  }
}

const toggleMindMapNode = data => {
  const ownerId = data?.kind === 'collapse-placeholder' ? data.payload?.owner_id : data?.id
  if (!ownerId) return
  const sourceNode = (() => {
    const find = current => {
      if (!current) return null
      if (current.id === ownerId) return current
      for (const child of current.children || []) {
        const match = find(child)
        if (match) return match
      }
      return null
    }
    return find(mindMapBase.value)
  })()
  if (!sourceNode?.children?.length) return
  const nextCollapsed = data?.kind === 'collapse-placeholder' ? false : !Boolean(data.folded)
  const updated = new Map(collapsePreferences.value)
  updated.set(ownerId, nextCollapsed)
  collapsePreferences.value = updated
  if (!nextCollapsed) hydrateDirectChildThumbnails(sourceNode)
}

const handleChartClick = params => {
  if (isMindMapDragging.value) return
  const data = params.data
  if (!data) return
  selectedMindMapNodeId.value = data.kind === 'state' ? data.id : data.payload?.owner_id || ''
  if (data.kind === 'state') mindMapSearchId.value = data.id
  if (data.kind === 'collapse-placeholder') {
    toggleMindMapNode(data)
    return
  }
  if (data.kind === 'state') openNode(data.payload, data.incomingTransition)
  else if (data.kind === 'action-leaf') openAction(data.payload)
  else if (data.kind === 'reference') {
    openAction(data.payload?.transition)
  }
}

const handleChartDoubleClick = params => {
  if (isMindMapDragging.value) return
  const data = params?.data
  if (!data) return
  if (data.kind === 'state' || data.kind === 'collapse-placeholder') toggleMindMapNode(data)
  if (data.kind === 'reference' && data.payload?.state_id) {
    void locateMindMapNode(`state-${data.payload.state_id}`)
  }
}

const collapseAllMindMapNodes = () => {
  const updated = new Map(collapsePreferences.value)
  collectExpandableMindMapNodes(modeMindMapBase.value).forEach(item => {
    updated.set(item.id, item.kind !== 'virtual-root')
  })
  collapsePreferences.value = updated
}

const expandNextMindMapLayer = () => {
  const placeholders = flattenVisibleMindMap(mindMapTree.value)
    .filter(item => item.kind === 'collapse-placeholder' && item.payload?.owner_id)
  const nextLevel = Math.min(...placeholders.map(item => Number(item.level) || 0))
  const ownerIds = [...new Set(
    placeholders
      .filter(item => (Number(item.level) || 0) === nextLevel)
      .map(item => item.payload.owner_id),
  )].slice(0, 12)
  if (!ownerIds.length) return ElMessage.info('当前视图已没有收起的下级页面')
  const updated = new Map(collapsePreferences.value)
  const expandableById = new Map(
    collectExpandableMindMapNodes(modeMindMapBase.value).map(item => [item.id, item]),
  )
  ownerIds.forEach(ownerId => {
    updated.set(ownerId, false)
    hydrateDirectChildThumbnails(expandableById.get(ownerId))
  })
  collapsePreferences.value = updated
}

const loadXml = async () => {
  if (!hasXmlEvidence.value || xmlText.value) return
  detailLoading.value = true
  try {
    const evidence = activeEvidence.value || {}
    const assetId = evidence.xml_asset_id
      || evidence.representative_xml_asset_id
      || selectedNode.value?.xml_asset_id
      || selectedNode.value?.representative_xml_asset_id
    const path = evidence.xml_path || selectedNode.value?.xml_path
    const response = await fetchStoredOrLegacyAsset(assetId, path, 'text')
    xmlText.value = String(response.data || '')
  } catch (error) {
    if (error.response?.status === 410) assetNotice.value = 'XML 已按保留策略清理，仅保留元数据和哈希。'
    else ElMessage.error(error.response?.data?.detail || error.message || 'XML 加载失败')
  } finally {
    detailLoading.value = false
  }
}

const parseActionBounds = action => {
  const bounds = action?.bounds || action?.target_bounds
  if (Array.isArray(bounds) && bounds.length >= 4) {
    const values = bounds.slice(0, 4).map(Number)
    return values.every(Number.isFinite) ? values : null
  }
  if (bounds && typeof bounds === 'object') {
    const values = [bounds.left ?? bounds.x1, bounds.top ?? bounds.y1, bounds.right ?? bounds.x2, bounds.bottom ?? bounds.y2].map(Number)
    return values.every(Number.isFinite) ? values : null
  }
  const matched = String(bounds || '').match(/\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]/)
  return matched ? matched.slice(1).map(Number) : null
}

const actionMapWidth = computed(() => Number(actionMap.value.screen_size?.width) || 0)
const actionMapHeight = computed(() => Number(actionMap.value.screen_size?.height) || 0)
const actionMapReady = computed(() => actionMapWidth.value > 0 && actionMapHeight.value > 0)
const detailDisplayWidth = computed(() => detailImageSize.value.width || actionMapWidth.value || 9)
const detailDisplayHeight = computed(() => detailImageSize.value.height || actionMapHeight.value || 20)
const actionStroke = action => {
  const status = inspectionActionStatus(action)
  if (status === 'BLOCKED') return '#f56c6c'
  if (['ERROR', 'ACTION_ERROR'].includes(status)) return '#d03050'
  if (status === 'NO_EFFECT') return '#909399'
  if (INSPECTION_LOCATOR_FAILURE_STATUSES.has(status) || action?.coordinate_only) return '#e6a23c'
  if (action?.invoked || ['PASS', 'SELF_LOOP'].includes(status)) return '#909399'
  return '#67c23a'
}
const actionDash = action => {
  const status = inspectionActionStatus(action)
  return action?.coordinate_only || INSPECTION_LOCATOR_FAILURE_STATUSES.has(status)
    || ['BLOCKED', 'SKIPPED', 'NOT_REACHED', 'CANCELLED', 'BUDGET_NOT_REACHED', 'COVERED_BY_FAMILY', 'FILTERED_NON_ACTIONABLE', 'QUEUE_TRUNCATED', 'CYCLE_CONVERGED'].includes(status)
    ? '8 5' : ''
}
const actionMapLabel = action => action?.label || action?.target_label || action?.action_type || '操作'
const actionStatusLabel = action => inspectionActionStatusMeta(action).label
const actionOrder = (action, fallback = null) => action?.display_order || action?.local_order || action?.order || fallback
const locatorForAction = item => item?.target_meta?.used_locator
  || item?.locator_candidates?.[0]?.by
  || (item?.coordinate_only ? 'coordinate' : '-')
const selectedActionLocator = computed(() => locatorForAction(selectedAction.value))
const selectedNodeReachability = computed(() => inspectionReachabilityEvidence(selectedNode.value || {}))
const selectedNodeReplay = computed(() => inspectionReplayEligibility(
  selectedNode.value || {},
  (graph.value.links || []).filter(link => Number(link.source ?? link.from_state_id) === Number(selectedNode.value?.state_id)),
))
const selectedNodeBoundaryText = computed(() => {
  const boundaries = selectedNode.value?.terminal_boundaries || []
  const directOutcome = selectedNode.value?.terminal_outcome
  const evidence = inspectionBoundaryEvidenceLabel(selectedNode.value?.boundary_evidence)
  if (!boundaries.length && directOutcome) {
    return [inspectionTerminalOutcomeLabel(directOutcome), evidence].filter(Boolean).join(' · ')
  }
  if (!boundaries.length) return '无安全或故障边界'
  return boundaries.map(item => [
    inspectionTerminalOutcomeLabel(item.terminal_outcome || item.outcome || item.type),
    inspectionBoundaryEvidenceLabel(item.boundary_evidence),
  ].filter(Boolean).join(' · ')).join('、')
})
const selectedNodeHasBoundary = computed(() => {
  const boundaries = selectedNode.value?.terminal_boundaries || []
  const outcome = String(selectedNode.value?.terminal_outcome || '').toUpperCase()
  return boundaries.length > 0 || Boolean(outcome && outcome !== 'NONE')
})
const selectedNodeDisplayName = computed(() => inspectionPageDisplayName(
  selectedNode.value,
  pageLabelForStateId(selectedNode.value?.state_id),
))
const selectedCaptureTime = computed(() => formatTime(
  selectedNode.value?.last_observed_at
    || selectedObservation.value?.captured_at
    || selectedObservation.value?.created_at
))
const observationOrdinal = (observation, index) => inspectionObservationOrdinal({
  total: observationsTotal.value,
  page: observationPage.value,
  pageSize: observationPageSize,
  index: index ?? observations.value.findIndex(item => Number(item.id) === Number(observation?.id)),
})
const actionTypeLabel = action => ({
  click: '点击',
  tap: '点击',
  scroll: '滚动',
  input: '输入',
  back: '返回',
}[String(action?.action_type || '').toLowerCase()] || action?.action_type || '页面操作')
const drawerTitle = computed(() => selectedAction.value ? '动作详情' : '页面详情')

const openTransitionObservation = kind => {
  const transition = selectedAction.value
  if (!transition) return
  const source = kind === 'source'
  const stateId = source
    ? transition.source ?? transition.from_state_id
    : transition.target ?? transition.to_state_id
  const observationId = source
    ? transition.source_observation_id
    : transition.target_observation_id
  const node = nodeByStateId.value.get(Number(stateId))
  if (node) void openNode(node, transition, observationId)
}

const openFaultAsset = async (assetId, path, responseType = 'blob') => {
  if (!assetId && !path) return
  try {
    const response = await fetchStoredOrLegacyAsset(assetId, path, responseType)
    if (responseType === 'text') {
      const blob = new Blob([String(response.data || '')], { type: 'text/plain;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      objectUrls.add(url)
      window.open(url, '_blank', 'noopener')
      return
    }
    const url = URL.createObjectURL(response.data)
    objectUrls.add(url)
    window.open(url, '_blank', 'noopener')
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || '故障产物加载失败')
  }
}

const saveSelection = async () => {
  try {
    await api.updateInspectionSelection(runId, selectedIds.value)
    ElMessage.success('稳定状态回归选择已保存')
    await fetchData(true)
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || '保存失败')
  }
}

const cancelRun = async () => {
  try {
    await api.cancelInspectionRun(runId)
    ElMessage.success('已请求取消')
    await fetchData(true)
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || '取消失败')
  }
}

const openInstalledReplay = () => {
  const branch = branchFilter.value || branchOptions.value[0] || ''
  router.push({
    path: '/special/compatibility/run',
    query: {
      inspection_run_id: String(runId),
      ...(branch ? { branch_key: branch } : {}),
    },
  })
}

const exportPng = async () => {
  await nextTick()
  const viewport = mindMapViewportRef.value
  const chartDom = chartRef.value?.getDom?.()
  const sourceCanvas = chartDom?.querySelector?.('canvas')
  if (!viewport || !sourceCanvas) return ElMessage.warning('当前图谱尚未渲染完成')

  const logicalX = Math.max(0, viewport.scrollLeft / mindMapScale.value)
  const logicalY = Math.max(0, viewport.scrollTop / mindMapScale.value)
  const logicalWidth = Math.max(1, Math.min(mindMapCanvasWidth.value - logicalX, viewport.clientWidth / mindMapScale.value))
  const logicalHeight = Math.max(1, Math.min(mindMapCanvasHeight.value - logicalY, viewport.clientHeight / mindMapScale.value))
  const exportSize = boundedMindMapExportSize({
    width: viewport.clientWidth,
    height: viewport.clientHeight,
    pixelRatio: Math.min(2, window.devicePixelRatio || 1),
  })
  const output = document.createElement('canvas')
  output.width = exportSize.width
  output.height = exportSize.height
  const context = output.getContext('2d')
  if (!context) return ElMessage.warning('浏览器无法导出当前视图')
  context.fillStyle = '#ffffff'
  context.fillRect(0, 0, output.width, output.height)
  const sourceScaleX = sourceCanvas.width / mindMapCanvasWidth.value
  const sourceScaleY = sourceCanvas.height / mindMapCanvasHeight.value
  const destinationWidth = output.width * Math.min(1, logicalWidth * mindMapScale.value / viewport.clientWidth)
  const destinationHeight = output.height * Math.min(1, logicalHeight * mindMapScale.value / viewport.clientHeight)
  context.drawImage(
    sourceCanvas,
    logicalX * sourceScaleX,
    logicalY * sourceScaleY,
    Math.max(1, logicalWidth * sourceScaleX),
    Math.max(1, logicalHeight * sourceScaleY),
    0,
    0,
    destinationWidth,
    destinationHeight,
  )
  output.toBlob(blob => {
    if (!blob) return ElMessage.warning('当前视图导出失败')
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `inspection-${runId}-${branchFilter.value || 'all'}-current-view.png`
    link.click()
    URL.revokeObjectURL(url)
  }, 'image/png')
}

const formatTime = value => value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-'
const statusText = value => inspectionRunStatusLabel({
  status: value,
  stop_reason: run.value?.stop_reason,
})
const formatEvidence = value => {
  if (!value) return '-'
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value)
  } catch {
    return '-'
  }
}
const applyMindMapViewport = ({ scale, scrollLeft, scrollTop }, behavior = 'auto') => {
  const viewport = mindMapViewportRef.value
  if (!viewport) return
  if (scale) mindMapScale.value = scale
  void nextTick(() => viewport.scrollTo({ left: scrollLeft, top: scrollTop, behavior }))
}
const zoomMindMap = factor => {
  const viewport = mindMapViewportRef.value
  if (!viewport) return
  applyMindMapViewport(zoomMindMapViewport({
    scale: mindMapScale.value,
    factor,
    scrollLeft: viewport.scrollLeft,
    scrollTop: viewport.scrollTop,
    viewportWidth: viewport.clientWidth,
    viewportHeight: viewport.clientHeight,
    contentWidth: mindMapCanvasWidth.value,
    contentHeight: mindMapCanvasHeight.value,
  }))
}
const fitMindMap = async () => {
  await nextTick()
  const viewport = mindMapViewportRef.value
  if (!viewport) return
  mindMapHasInitialFocus.value = true
  applyMindMapViewport(fitMindMapViewport({
    viewportWidth: viewport.clientWidth,
    viewportHeight: viewport.clientHeight,
    contentWidth: mindMapCanvasWidth.value,
    contentHeight: mindMapCanvasHeight.value,
  }))
}
const flattenVisibleMindMap = root => {
  const result = []
  const visit = node => {
    result.push(node)
    ;(node?.children || []).forEach(visit)
  }
  visit(root)
  return result
}
const findMindMapNode = (root, targetId, ancestors = []) => {
  if (!root) return null
  if (root.id === targetId || (root.payload?.aggregated_state_ids || []).some(id => `state-${id}` === targetId)) {
    return { node: root, ancestors }
  }
  for (const child of root.children || []) {
    const match = findMindMapNode(child, targetId, [...ancestors, root])
    if (match) return match
  }
  return null
}
const revealMindMapNode = async targetId => {
  const match = findMindMapNode(modeMindMapBase.value, targetId)
  if (!match) return targetId
  const updated = new Map(collapsePreferences.value)
  match.ancestors.forEach(item => updated.set(item.id, false))
  collapsePreferences.value = updated
  await nextTick()
  await new Promise(resolve => window.requestAnimationFrame(resolve))
  return match.node.id
}
const renderedMindMapNode = targetId => {
  const visible = flattenVisibleMindMap(mindMapTree.value)
  const dataIndex = visible.findIndex(item => item.id === targetId)
  if (dataIndex < 0) return null
  const point = inspectionMindMapNodePositions(mindMapTree.value, {
    width: mindMapCanvasWidth.value,
    height: mindMapCanvasHeight.value,
    top: 72,
  }).get(targetId)
  return point ? { ...point, dataIndex } : null
}
const locateMindMapNode = async (requestedId = '', {
  behavior = 'smooth',
  syncSearch = true,
} = {}) => {
  await nextTick()
  const visible = flattenVisibleMindMap(mindMapTree.value)
  const defaultId = currentReportStateId.value ? `state-${currentReportStateId.value}` : ''
  const targetId = requestedId || mindMapSearchId.value || selectedMindMapNodeId.value || defaultId
    || visible.find(item => item.kind === 'state' && item.payload?.tree_role === 'home')?.id
  if (!targetId) return
  const visibleTargetId = await revealMindMapNode(targetId)
  const rendered = renderedMindMapNode(visibleTargetId)
  const viewport = mindMapViewportRef.value
  if (!rendered || !viewport) return ElMessage.warning('当前筛选条件下未找到该页面')
  const position = centerMindMapViewport({
    pointX: rendered.x,
    pointY: rendered.y,
    scale: mindMapScale.value,
    viewportWidth: viewport.clientWidth,
    viewportHeight: viewport.clientHeight,
    contentWidth: mindMapCanvasWidth.value,
    contentHeight: mindMapCanvasHeight.value,
  })
  viewport.scrollTo(mindMapScrollOptions(position, behavior))
  selectedMindMapNodeId.value = visibleTargetId
  if (syncSearch) mindMapSearchId.value = targetId
}
const locateMindMapHome = () => {
  if (homeMindMapStateId.value) {
    void locateMindMapNode(`state-${homeMindMapStateId.value}`)
  }
}
const handleMindMapSearch = value => {
  if (value) void locateMindMapNode(value)
}
const handleMindMapWheel = event => zoomMindMap(event.deltaY < 0 ? 1.16 : 1 / 1.16)
const handleMindMapPointerDown = event => {
  if (event.button !== 0) return
  const viewport = mindMapViewportRef.value
  if (!viewport) return
  mindMapPointer = {
    id: event.pointerId,
    x: event.clientX,
    y: event.clientY,
    left: viewport.scrollLeft,
    top: viewport.scrollTop,
  }
}
const handleMindMapPointerMove = event => {
  if (!mindMapPointer || mindMapPointer.id !== event.pointerId) return
  const viewport = mindMapViewportRef.value
  if (!viewport) return
  const dx = event.clientX - mindMapPointer.x
  const dy = event.clientY - mindMapPointer.y
  if (!isMindMapDragging.value && Math.hypot(dx, dy) < 4) return
  if (!isMindMapDragging.value) viewport.setPointerCapture?.(event.pointerId)
  isMindMapDragging.value = true
  event.preventDefault()
  viewport.scrollLeft = mindMapPointer.left - dx
  viewport.scrollTop = mindMapPointer.top - dy
}
const handleMindMapPointerUp = event => {
  if (!mindMapPointer || mindMapPointer.id !== event.pointerId) return
  const viewport = mindMapViewportRef.value
  if (viewport?.hasPointerCapture?.(event.pointerId)) {
    viewport.releasePointerCapture?.(event.pointerId)
  }
  mindMapPointer = null
  window.setTimeout(() => { isMindMapDragging.value = false }, 0)
}
const updateResponsiveLayout = () => {
  const viewport = mindMapViewportRef.value
  if (!viewport) return
  viewport.scrollLeft = Math.min(viewport.scrollLeft, Math.max(0, scaledMindMapWidth.value - viewport.clientWidth))
  viewport.scrollTop = Math.min(viewport.scrollTop, Math.max(0, scaledMindMapHeight.value - viewport.clientHeight))
}

const initialiseMindMapFocus = async () => {
  await nextTick()
  const viewport = mindMapViewportRef.value
  if (!viewport) return
  mindMapScale.value = focusedMindMapScale({ viewportWidth: viewport.clientWidth })
  mindMapHasInitialFocus.value = true
  await nextTick()
  await locateMindMapNode(initialMindMapFocusId.value, {
    behavior: 'auto',
    syncSearch: false,
  })
}

watch(activeTab, value => {
  if (value === 'page-tree' && !mindMapHasInitialFocus.value) void initialiseMindMapFocus()
})

watch([mindMapCanvasWidth, mindMapCanvasHeight], async ([nextWidth, nextHeight], [previousWidth, previousHeight]) => {
  const viewport = mindMapViewportRef.value
  if (!viewport || !mindMapHasInitialFocus.value || !previousWidth || !previousHeight) return
  const normalizedX = (viewport.scrollLeft + viewport.clientWidth / 2) / (previousWidth * mindMapScale.value)
  const normalizedY = (viewport.scrollTop + viewport.clientHeight / 2) / (previousHeight * mindMapScale.value)
  await nextTick()
  const centered = centerMindMapViewport({
    pointX: normalizedX * nextWidth,
    pointY: normalizedY * nextHeight,
    scale: mindMapScale.value,
    viewportWidth: viewport.clientWidth,
    viewportHeight: viewport.clientHeight,
    contentWidth: nextWidth,
    contentHeight: nextHeight,
  })
  viewport.scrollLeft = centered.scrollLeft
  viewport.scrollTop = centered.scrollTop
})

onMounted(async () => {
  window.addEventListener('resize', updateResponsiveLayout, { passive: true })
  await fetchData()
  pollTimer = window.setInterval(() => {
    if (run.value && !terminal(run.value.status)) fetchData(true)
  }, 5000)
})

onBeforeUnmount(() => {
  componentUnmounted = true
  detailRequestId += 1
  if (pollTimer) window.clearInterval(pollTimer)
  window.removeEventListener('resize', updateResponsiveLayout)
  thumbnailRetryTimers.forEach(timer => window.clearTimeout(timer))
  thumbnailRetryTimers.clear()
  objectUrls.forEach(url => URL.revokeObjectURL(url))
  objectUrls.clear()
})
</script>

<template>
  <div class="inspection-report" v-loading="loading">
    <div class="toolbar">
      <div class="toolbar-left">
        <el-button :icon="ArrowLeft" text @click="router.back()">返回</el-button>
        <div>
          <div class="title">{{ run?.name || `巡检 #${runId}` }}</div>
          <div class="subtitle">{{ run?.package_name }} · {{ formatTime(run?.started_at || run?.created_at) }}</div>
        </div>
        <el-tag v-if="run" :type="runStatusTagType(run.status)" effect="plain">{{ statusText(run.status) }}</el-tag>
      </div>
      <div class="toolbar-right">
        <el-button :icon="Refresh" @click="fetchData()">刷新</el-button>
        <el-button :icon="Download" @click="exportPng">导出当前视图</el-button>
        <el-button
          v-if="canOpenInstalledReplay"
          type="primary"
          plain
          :icon="VideoPlay"
          @click="openInstalledReplay"
        >升级后回放</el-button>
        <el-button v-if="run && !terminal(run.status)" type="danger" plain :icon="VideoPause" @click="cancelRun">取消任务</el-button>
      </div>
    </div>

    <div v-if="run && terminal(run.status)" class="stats">
      <div>
        <span>核心旅程</span>
        <strong :class="coverageMetricClass">
          {{ businessCoverage.available ? `${businessCoverage.covered}/${businessCoverage.total}` : '未评估' }}
        </strong>
        <small v-if="businessCoverage.available">
          业务线 {{ businessCoverage.scopeSelected }}/{{ businessCoverage.scopeTotal }} · {{ inspectionCoverageVerdictLabel(businessCoverage.selectedScopeVerdict) }}
        </small>
        <small v-else>当前报告没有冻结的业务覆盖清单</small>
      </div>
      <div>
        <span class="metric-label">
          应用面覆盖
          <el-tooltip content="分母是跨 run 累积的应用面总数，不是本次发现的页面数。判定门槛看累积值，本次值仅供参考" placement="top">
            <el-icon aria-label="应用面覆盖说明"><InfoFilled /></el-icon>
          </el-tooltip>
        </span>
        <strong :class="surfaceMetricClass">
          {{ surfaceCoverage.available ? `${surfaceCoverage.runVisited}/${surfaceCoverage.known}` : '-' }}
        </strong>
        <small v-if="surfaceCoverage.available">
          近 {{ surfaceCoverage.windowDays }} 天累计 {{ surfaceCoverage.cumulativeCovered }}/{{ surfaceCoverage.known }} · {{ inspectionCoverageVerdictLabel(surfaceCoverage.verdict) }}
        </small>
        <small v-else>应用地图为空，需先回填历史 run</small>
      </div>
      <div>
        <span>残留未覆盖</span>
        <strong :class="surfaceGapCount ? 'danger' : 'success'">
          {{ surfaceCoverage.available ? surfaceGapCount : '-' }}
        </strong>
        <small v-if="surfaceCoverage.available">
          从未覆盖 {{ surfaceCoverage.neverCovered.length }} · 超窗 {{ surfaceCoverage.stale.length }} · 动作槽位 {{ surfaceCoverage.slots.neverCovered }}/{{ surfaceCoverage.slots.total }} 未覆盖
        </small>
        <small v-else>无法统计残留</small>
      </div>
      <div>
        <span class="metric-label">
          证据质量
          <el-tooltip content="综合 XML 可读性、终点复验、未知页面和覆盖契约冲突" placement="top">
            <el-icon aria-label="证据质量说明"><InfoFilled /></el-icon>
          </el-tooltip>
        </span>
        <strong :class="evidenceQualityClass">{{ inspectionEvidenceQualityLabel(businessCoverage.evidenceQuality) }}</strong>
        <small>{{ coverageBlindSpots.length ? `${coverageBlindSpots.length} 个显著盲区` : '未发现显著证据盲区' }}</small>
      </div>
    </div>
    <div v-else class="running-summary" aria-label="巡检实时进度">
      <strong>{{ runPhase }}</strong>
      <span>{{ currentReportPage }}</span>
      <span>{{ currentReportAction }}</span>
      <span v-if="frontierCounts.pending !== null">待执行 {{ frontierCounts.pending }}</span>
      <span v-if="run?.elapsed_seconds !== undefined">{{ Math.round(run.elapsed_seconds / 60) }} / {{ Math.round((run.duration_seconds || 0) / 60) }} 分钟</span>
    </div>
    <div v-if="run && terminal(run.status) && coverageBlindSpots.length" class="coverage-blind-spots" role="alert">
      <strong>覆盖盲区</strong>
      <span v-for="(spot, index) in coverageBlindSpots" :key="`${spot.type}-${spot.branch_key || ''}-${index}`">
        {{ spot.message || spot.type }}<template v-if="spot.count">（{{ spot.count }}）</template>
      </span>
    </div>
    <el-collapse v-model="diagnosticPanels" class="diagnostic-collapse">
      <el-collapse-item name="diagnostics" title="运行诊断">
        <div class="diagnostics-grid">
          <span v-for="([label, value], index) in diagnostics" :key="`${label}-${index}`">
            <em>{{ label }}</em><strong>{{ value }}</strong>
          </span>
        </div>
      </el-collapse-item>
    </el-collapse>

    <el-tabs v-model="activeTab" class="report-tabs">
      <el-tab-pane label="实时巡检" name="live">
        <InspectionLivePanel
          v-if="run"
          :run-id="runId"
          :run-status="run?.status || 'PENDING'"
          :active="activeTab === 'live'"
          :state-id="terminal(run?.status) ? terminalReviewState?.state_id || '' : ''"
          :fallback-screenshot-path="terminalReviewState?.screenshot_path || ''"
          :fallback-screenshot-asset-id="terminalReviewState?.screenshot_asset_id || terminalReviewState?.representative_screenshot_asset_id || ''"
          :page-label="inspectionPageDisplayName(
            nodeByStateId.get(Number(terminal(run?.status) ? terminalReviewState?.state_id : liveStateId)),
            terminal(run?.status) ? terminalReviewState?.display_label : '',
          )"
          @state-select="liveStateId = $event"
          @terminal="fetchData(true)"
        />
      </el-tab-pane>

      <el-tab-pane v-if="businessCoverage.available" label="核心旅程" name="coverage">
        <div class="coverage-tab">
          <div class="coverage-manifest-line">
            <span>{{ businessCoverage.manifest?.id || '覆盖清单' }} · v{{ businessCoverage.manifest?.version || '-' }}</span>
            <el-tooltip :content="businessCoverage.manifest?.hash || '-'" placement="top">
              <code>{{ businessCoverage.manifest?.hash ? businessCoverage.manifest.hash.slice(0, 12) : '-' }}</code>
            </el-tooltip>
            <el-tag v-if="businessCoverage.origin === 'BACKFILLED_V1'" type="info" effect="plain">历史 v1 回填</el-tag>
          </div>
          <section v-if="surfaceCoverage.available" class="coverage-branch surface-gap">
            <header>
              <h3>应用面覆盖</h3>
              <span>
                分母 {{ surfaceCoverage.known }} 个已知面（跨 run 累积）·
                本次访问 {{ surfaceCoverage.runVisited }} ·
                本次完整覆盖 {{ surfaceCoverage.runFullyCovered }} ·
                近 {{ surfaceCoverage.windowDays }} 天累计完整覆盖 {{ surfaceCoverage.cumulativeCovered }}
              </span>
            </header>
            <p v-if="surfaceCoverage.unclassified" class="surface-gap-note">
              其中 {{ surfaceCoverage.unclassified }} 个面无法被识别为已知业务页面，本身即为盲区。
            </p>
            <el-table
              v-if="surfaceCoverage.neverCovered.length"
              :data="surfaceCoverage.neverCovered"
              size="small"
              border
            >
              <el-table-column label="从未覆盖的面" prop="page_subtype" min-width="160">
                <template #default="{ row }">{{ row.label || row.page_subtype }}</template>
              </el-table-column>
              <el-table-column label="动作槽位" prop="action_slot_count" width="100" />
              <el-table-column label="首次发现" prop="first_seen_run_id" width="110">
                <template #default="{ row }">Run {{ row.first_seen_run_id || '-' }}</template>
              </el-table-column>
              <el-table-column label="最近出现" prop="last_seen_run_id" width="110">
                <template #default="{ row }">Run {{ row.last_seen_run_id || '-' }}</template>
              </el-table-column>
            </el-table>
            <p v-else class="surface-gap-note">所有已知面都至少被覆盖过一次。</p>
            <el-table
              v-if="surfaceCoverage.stale.length"
              :data="surfaceCoverage.stale"
              size="small"
              border
            >
              <el-table-column label="超出时间窗的面" prop="page_subtype" min-width="160">
                <template #default="{ row }">{{ row.label || row.page_subtype }}</template>
              </el-table-column>
              <el-table-column label="未覆盖槽位" prop="stale_slot_count" width="110" />
              <el-table-column label="最早覆盖时间" prop="oldest_covered_at" min-width="170">
                <template #default="{ row }">{{ row.oldest_covered_at || '从未' }}</template>
              </el-table-column>
            </el-table>
          </section>
          <section v-for="branch in coverageBranches" :key="branch.branch_key" class="coverage-branch">
            <header>
              <h3>{{ branchDisplayLabel(branch.branch_key) }}</h3>
              <el-tag :type="branch.verdict === 'COMPLETE' ? 'success' : branch.verdict === 'NOT_IN_SCOPE' ? 'info' : branch.verdict === 'INCONCLUSIVE' ? 'warning' : 'danger'" effect="plain">
                {{ inspectionCoverageVerdictLabel(branch.verdict) }}
              </el-tag>
              <span v-if="branch.selected">必达 {{ branch.covered_required }}/{{ branch.total_required }}</span>
              <span v-else>本次未运行</span>
            </header>
            <el-table v-if="branch.selected" :data="branch.items || []" size="small" table-layout="fixed">
              <el-table-column prop="label" label="旅程" min-width="180">
                <template #default="{ row }">
                  <span>{{ row.label }}</span>
                  <el-tag v-if="!row.required" size="small" type="info" effect="plain">可选</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="结果" width="104">
                <template #default="{ row }">
                  <el-tag size="small" :type="inspectionCoverageItemStatusMeta(row.status).type" effect="plain">
                    {{ inspectionCoverageItemStatusMeta(row.status).label }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="进度" width="88" align="center">
                <template #default="{ row }">
                  {{ row.total_stages > 0 ? `${row.deepest_stage || 0}/${row.total_stages}` : '-' }}
                </template>
              </el-table-column>
              <el-table-column label="证据" min-width="190">
                <template #default="{ row }">
                  <div v-if="evidenceStateNodes(row).length || evidenceTransitions(row).length" class="coverage-evidence-links">
                    <span v-if="evidenceStateNodes(row).length" class="coverage-evidence-group">
                      <em>页面</em>
                      <el-button
                        v-for="node in evidenceStateNodes(row)"
                        :key="node.state_id"
                        link
                        type="primary"
                        @click="openCoverageState(node)"
                      >{{ node.display_label || pageLabelForStateId(node.state_id) }}</el-button>
                    </span>
                    <span v-if="evidenceTransitions(row).length" class="coverage-evidence-group">
                      <em>动作</em>
                      <code v-for="transition in evidenceTransitions(row)" :key="transition.id">
                        {{ evidenceTransitionLabel(transition) }}
                      </code>
                    </span>
                  </div>
                  <span v-else>-</span>
                </template>
              </el-table-column>
              <el-table-column label="判定" min-width="300" show-overflow-tooltip>
                <template #default="{ row }">{{ inspectionCoverageItemReason(row) }}</template>
              </el-table-column>
            </el-table>
          </section>
        </div>
      </el-tab-pane>

      <el-tab-pane v-if="scopeCoverageBranches.length" label="单页覆盖" name="scope-coverage">
        <div class="coverage-tab">
          <section v-for="branch in scopeCoverageBranches" :key="branch.branch_key" class="coverage-branch">
            <header>
              <h3>{{ branch.branch_name || branch.branch_key }}</h3>
              <el-tag type="info" effect="plain">单页穷举</el-tag>
              <span>
                识别动作 {{ branch.total_actions }} ·
                已执行 {{ branch.executed_actions }} ·
                跳过 {{ branch.skipped_actions }}（安全拦截 {{ branch.safety_blocked_actions }}）·
                页内覆盖率 {{ (branch.coverage_ratio * 100).toFixed(1) }}%
              </span>
            </header>
            <el-table v-if="scopeSkipRows(branch).length" :data="scopeSkipRows(branch)" size="small" border>
              <el-table-column label="跳过原因" min-width="180">
                <template #default="{ row }">{{ row.label }}</template>
              </el-table-column>
              <el-table-column label="次数" prop="count" width="90" />
            </el-table>
            <p v-else class="surface-gap-note">入口页面上识别到的动作全部执行完毕。</p>
            <el-table
              v-if="(branch.unconfigured_surfaces || []).length"
              :data="branch.unconfigured_surfaces"
              size="small"
              border
            >
              <el-table-column label="已发现未配置的页面" min-width="180">
                <template #default="{ row }">{{ row.title }}</template>
              </el-table-column>
              <el-table-column label="Activity" prop="activity" min-width="200" show-overflow-tooltip />
              <el-table-column label="到达页面" min-width="170">
                <template #default="{ row }">
                  <el-button
                    v-for="node in scopeSurfaceStateNodes(row)"
                    :key="node.state_id"
                    link
                    type="primary"
                    @click="openCoverageState(node)"
                  >{{ node.display_label || pageLabelForStateId(node.state_id) }}</el-button>
                  <span v-if="!scopeSurfaceStateNodes(row).length">-</span>
                </template>
              </el-table-column>
              <el-table-column label="配置状态" width="170">
                <template #default="{ row }">
                  <el-tag v-if="row.configured_branch_key" size="small" type="success" effect="plain">
                    已配置：{{ branchDisplayLabel(row.configured_branch_key) }}
                  </el-tag>
                  <el-tag v-else size="small" type="warning" effect="plain">待配置入口用例</el-tag>
                </template>
              </el-table-column>
            </el-table>
            <p v-else class="surface-gap-note">本次未发现跳出该页面且未配置的去向页面。</p>
          </section>
        </div>
      </el-tab-pane>

      <el-tab-pane label="页面树状图" name="page-tree">
        <div class="filters">
          <el-select v-model="branchFilter" clearable placeholder="全部业务线" style="width: 180px">
            <el-option v-for="branch in branchOptions" :key="branch" :label="branchDisplayLabel(branch)" :value="branch" />
          </el-select>
          <el-segmented
            v-model="treeMode"
            :options="[
              { label: '关键路径', value: 'key' },
              { label: '全部页面', value: 'all' },
            ]"
          />
          <el-segmented
            v-model="stateFilter"
            :options="[
              { label: '全部', value: 'all' },
              { label: '可回放', value: 'replayable' },
              { label: '需关注', value: 'attention' },
            ]"
          />
          <div class="tree-legend" aria-label="页面树图例">
            <span><i class="legend-dot is-home"></i>首页</span>
            <span><i class="legend-dot is-page"></i>下游页面</span>
            <span><i class="legend-dot is-viewport"></i>同页视口</span>
            <span><i class="legend-dot is-reference"></i>回环/引用</span>
          </div>
          <span class="subtitle mind-map-summary">{{ filteredNodes.length }} 个页面 · {{ filteredLinks.length }} 个转移</span>
        </div>
        <div class="mind-map-shell">
          <div class="mind-map-floating-tools" aria-label="页面树操作">
            <el-select
              v-model="mindMapSearchId"
              filterable
              clearable
              placeholder="搜索页面"
              class="mind-map-search"
              :suffix-icon="Search"
              @change="handleMindMapSearch"
            >
              <el-option v-for="item in mindMapSearchOptions" :key="item.id" :label="item.label" :value="item.id" />
            </el-select>
            <el-tooltip content="放大" placement="bottom"><el-button circle :icon="ZoomIn" aria-label="放大" @click="zoomMindMap(1.25)" /></el-tooltip>
            <el-tooltip content="缩小" placement="bottom"><el-button circle :icon="ZoomOut" aria-label="缩小" @click="zoomMindMap(1 / 1.25)" /></el-tooltip>
            <output class="mind-map-zoom-value" aria-live="polite">{{ Math.round(mindMapScale * 100) }}%</output>
            <el-tooltip content="定位当前或已选页面" placement="bottom"><el-button circle :icon="Aim" aria-label="定位当前或已选页面" @click="locateMindMapNode()" /></el-tooltip>
            <el-tooltip content="回到首页" placement="bottom"><el-button circle :icon="House" aria-label="回到首页" @click="locateMindMapHome" /></el-tooltip>
            <el-tooltip content="适应画布" placement="bottom"><el-button circle :icon="FullScreen" aria-label="适应画布" @click="fitMindMap" /></el-tooltip>
            <el-tooltip content="展开下一层" placement="bottom"><el-button circle :icon="Expand" aria-label="展开下一层" @click="expandNextMindMapLayer" /></el-tooltip>
            <el-tooltip content="收起深层页面" placement="bottom"><el-button circle :icon="Fold" aria-label="收起深层页面" @click="collapseAllMindMapNodes" /></el-tooltip>
          </div>
          <div
            ref="mindMapViewportRef"
            class="mind-map-viewport"
            :class="{ 'is-dragging': isMindMapDragging }"
            @wheel.ctrl.prevent="handleMindMapWheel"
            @pointerdown="handleMindMapPointerDown"
            @pointermove="handleMindMapPointerMove"
            @pointerup="handleMindMapPointerUp"
            @pointercancel="handleMindMapPointerUp"
          >
            <div
              class="mind-map-world"
              :style="{ width: `${scaledMindMapWidth}px`, height: `${scaledMindMapHeight}px` }"
            >
              <VChart
                ref="chartRef"
                class="mind-map-chart"
                :style="{
                  width: `${mindMapCanvasWidth}px`,
                  height: `${mindMapCanvasHeight}px`,
                  transform: `scale(${mindMapScale})`,
                }"
                :option="mindMapOption"
                :init-options="MIND_MAP_INIT_OPTIONS"
                autoresize
                @click="handleChartClick"
                @dblclick="handleChartDoubleClick"
              />
            </div>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="稳定回归资产" name="regression">
        <div class="selection-header">
          <el-alert title="优先选择已复验页面；安全拦截前的路径可作为安全前缀回放。" type="info" :closable="false" show-icon />
          <el-button type="primary" :icon="Select" @click="saveSelection">保存选择</el-button>
        </div>
        <el-checkbox-group v-model="selectedIds" class="state-grid">
          <el-checkbox v-for="state in stableStates" :key="state.state_id" :value="state.state_id" border>
            <span>{{ state.display_label || pageLabelForStateId(state.state_id) }} · {{ pageTitleForNode(state) }}</span>
            <small>{{ branchDisplayLabel(state.branch_key) }} · {{ inspectionReachabilityLabel(state.reachability_evidence) }} · {{ inspectionReplayEligibilityLabel(state.replay_eligibility) }}</small>
          </el-checkbox>
        </el-checkbox-group>
      </el-tab-pane>

      <el-tab-pane :label="`故障（${run?.fault_count || 0}）`" name="faults">
        <el-table :data="run?.faults || []" height="100%">
          <el-table-column prop="fault_type" label="类型" width="140" />
          <el-table-column prop="summary" label="摘要" min-width="260" />
          <el-table-column prop="occurrence_count" label="次数" width="80" align="center" />
          <el-table-column label="状态/时间" width="190">
            <template #default="{ row }">{{ pageLabelForStateId(row.state_id) }} · {{ formatTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="产物" min-width="220">
            <template #default="{ row }">
              <el-button v-if="row.screenshot_asset_id || row.screenshot_path" link type="primary" @click="openFaultAsset(row.screenshot_asset_id, row.screenshot_path)">截图</el-button>
              <el-button v-if="row.xml_asset_id || row.xml_path" link type="primary" @click="openFaultAsset(row.xml_asset_id, row.xml_path, 'text')">XML</el-button>
              <el-button v-if="row.full_log_asset_id || row.full_log_path" link type="primary" @click="openFaultAsset(row.full_log_asset_id, row.full_log_path, 'text')">日志</el-button>
              <el-button v-if="row.replay_asset_id || row.replay_path" link type="primary" @click="openFaultAsset(row.replay_asset_id, row.replay_path)">MP4</el-button>
              <el-button v-if="row.trace_asset_id || row.trace_path" link type="primary" @click="openFaultAsset(row.trace_asset_id, row.trace_path)">Trace</el-button>
              <span v-if="!row.screenshot_asset_id && !row.screenshot_path && !row.xml_asset_id && !row.xml_path && !row.full_log_asset_id && !row.full_log_path && !row.replay_asset_id && !row.replay_path && !row.trace_asset_id && !row.trace_path">-</span>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <el-drawer v-model="drawerVisible" size="560px" :title="drawerTitle">
      <div v-if="selectedNode" v-loading="detailLoading" class="drawer-content">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="页面" :span="2">{{ selectedNodeDisplayName }}</el-descriptions-item>
          <el-descriptions-item label="巡检结果">{{ inspectionReachabilityLabel(selectedNodeReachability) }}</el-descriptions-item>
          <el-descriptions-item label="升级回放">{{ inspectionReplayEligibilityLabel(selectedNodeReplay) }}</el-descriptions-item>
          <el-descriptions-item label="最近采集" :span="2">{{ selectedCaptureTime }}</el-descriptions-item>
          <el-descriptions-item v-if="selectedNodeHasBoundary" label="停止原因" :span="2">{{ selectedNodeBoundaryText }}</el-descriptions-item>
        </el-descriptions>
        <el-descriptions v-if="selectedIncomingAction" title="如何到达这里" :column="2" border size="small">
          <el-descriptions-item label="操作" :span="2">{{ actionTypeLabel(selectedIncomingAction) }}“{{ actionTargetLabel(selectedIncomingAction) }}”</el-descriptions-item>
          <el-descriptions-item label="执行结果">{{ actionStatusLabel(selectedIncomingAction) }}</el-descriptions-item>
          <el-descriptions-item label="来源页面">{{ pageLabelForStateId(selectedIncomingAction.source || selectedIncomingAction.from_state_id) }}</el-descriptions-item>
        </el-descriptions>
        <el-collapse class="technical-details">
          <el-collapse-item title="技术信息" name="technical">
            <el-descriptions :column="2" border size="small">
              <el-descriptions-item label="内部 State ID">{{ selectedNode.state_id }}</el-descriptions-item>
              <el-descriptions-item label="模板">{{ selectedNode.template_id ? `T${selectedNode.template_id}` : '-' }}</el-descriptions-item>
              <el-descriptions-item label="页面类型">{{ inspectionPageRoleLabel(selectedNode.page_role || selectedNode.template_role) }}</el-descriptions-item>
              <el-descriptions-item label="深度">{{ selectedNode.depth ?? '-' }}</el-descriptions-item>
              <el-descriptions-item label="定位质量">{{ selectedNode.locator_quality || '-' }}</el-descriptions-item>
              <el-descriptions-item label="同构族">{{ familyForNode(selectedNode)?.name || familyForNode(selectedNode)?.id || '-' }}</el-descriptions-item>
              <el-descriptions-item label="探索方式">{{ explorationModeLabel(selectedNode.exploration_mode) }}</el-descriptions-item>
              <el-descriptions-item label="业务线">{{ branchDisplayLabel(selectedNode.branch_key) }}</el-descriptions-item>
              <el-descriptions-item label="探索结果">{{ selectedNode.expansion_status ? expansionStatusLabel(selectedNode.expansion_status) : selectedNode.expanded_at ? '已展开' : selectedNode.queued_at ? '待展开' : '已采集' }}</el-descriptions-item>
              <el-descriptions-item label="采集次数">{{ selectedNode.observation_count ?? selectedNode.visit_count ?? 0 }}</el-descriptions-item>
              <el-descriptions-item label="页面动作">{{ selectedNode.non_navigation_actions?.length ?? selectedNode.action_summary?.count ?? '-' }}</el-descriptions-item>
              <template v-if="selectedIncomingAction">
                <el-descriptions-item label="Transition ID">{{ selectedIncomingAction.id || '-' }}</el-descriptions-item>
                <el-descriptions-item label="全局序号">#{{ selectedIncomingAction.sequence ?? '-' }}</el-descriptions-item>
                <el-descriptions-item label="定位方式">{{ locatorForAction(selectedIncomingAction) }}</el-descriptions-item>
                <el-descriptions-item label="Contract ID">{{ selectedIncomingAction.coverage_contract_id || '-' }}</el-descriptions-item>
              </template>
              <el-descriptions-item label="Activity" :span="2">{{ selectedNode.activity || '-' }}</el-descriptions-item>
              <el-descriptions-item label="语义键" :span="2">{{ selectedNode.semantic_key || '-' }}</el-descriptions-item>
              <el-descriptions-item label="实例锚点" :span="2">{{ selectedNode.instance_anchor || '-' }}</el-descriptions-item>
            </el-descriptions>
          </el-collapse-item>
        </el-collapse>
        <el-collapse v-if="observationsTotal || observationLoading" class="observation-collapse">
          <el-collapse-item :title="`采集记录（${observationsTotal}）`" name="observations">
            <section class="observation-section" v-loading="observationLoading">
          <div class="observation-section-header">
            <div class="detail-section-title">采集时间线</div>
            <el-button
              v-if="selectedObservation && Number(selectedObservation.id) !== representativeObservationId"
              :icon="Select"
              link
              type="primary"
              :loading="representativeSaving"
              @click="setRepresentativeObservation"
            >设为代表</el-button>
          </div>
          <el-timeline class="observation-timeline">
            <el-timeline-item
              v-for="(observation, index) in observations"
              :key="observation.id"
              :timestamp="formatTime(observation.captured_at || observation.created_at)"
              placement="top"
              :type="Number(selectedObservation?.id) === Number(observation.id) ? 'primary' : undefined"
              :hollow="Number(selectedObservation?.id) !== Number(observation.id)"
            >
              <button
                type="button"
                class="observation-entry"
                :class="{ 'is-active': Number(selectedObservation?.id) === Number(observation.id) }"
                @click="selectObservation(observation)"
              >
                <span class="observation-title">
                  第 {{ observationOrdinal(observation, index) }} 次采集 · {{ inspectionCaptureKindLabel(observation.capture_kind) }}
                  <el-tag v-if="Number(observation.id) === representativeObservationId || observation.is_representative" size="small" type="primary" effect="plain">代表</el-tag>
                </span>
                <span class="observation-tags">
                  <span>{{ inspectionAssetAvailabilityLabel(observation) }}</span>
                  <span v-if="observation.original_width && observation.original_height">{{ observation.original_width }} × {{ observation.original_height }}</span>
                </span>
              </button>
            </el-timeline-item>
          </el-timeline>
          <el-pagination
            v-if="observationsTotal > observationPageSize"
            small
            layout="prev, pager, next"
            :page-size="observationPageSize"
            :total="observationsTotal"
            :current-page="observationPage"
            @current-change="changeObservationPage"
          />
            </section>
        <el-descriptions v-if="selectedObservation" title="当前采集" :column="2" border size="small">
          <el-descriptions-item label="用途">{{ inspectionCaptureKindLabel(selectedObservation.capture_kind) }}</el-descriptions-item>
          <el-descriptions-item label="采集时间">{{ formatTime(selectedObservation.captured_at || selectedObservation.created_at) }}</el-descriptions-item>
          <el-descriptions-item label="可查看内容" :span="2">{{ inspectionAssetAvailabilityLabel(selectedObservation) }}</el-descriptions-item>
        </el-descriptions>
        <el-collapse v-if="selectedObservation" class="technical-details">
          <el-collapse-item title="当前采集的技术信息" name="observation-technical">
            <el-descriptions :column="2" border size="small">
              <el-descriptions-item label="Observation ID">{{ selectedObservation.id }}</el-descriptions-item>
              <el-descriptions-item label="采集类型">{{ selectedObservation.capture_kind || '-' }}</el-descriptions-item>
              <el-descriptions-item label="稳定依据">{{ selectedObservation.stable_by || '-' }}</el-descriptions-item>
              <el-descriptions-item label="资产等级">{{ selectedObservation.retention_class || '-' }}</el-descriptions-item>
              <el-descriptions-item label="资产状态">{{ selectedObservation.asset_status || '-' }}</el-descriptions-item>
              <el-descriptions-item label="匹配置信度">{{ selectedObservation.match_confidence ?? '-' }}</el-descriptions-item>
              <el-descriptions-item label="截图 Asset ID" :span="2">{{ selectedObservation.screenshot_asset_id || '-' }}</el-descriptions-item>
              <el-descriptions-item label="XML Asset ID" :span="2">{{ selectedObservation.xml_asset_id || '-' }}</el-descriptions-item>
              <el-descriptions-item label="匹配证据" :span="2">{{ formatEvidence(selectedObservation.match_evidence) }}</el-descriptions-item>
            </el-descriptions>
          </el-collapse-item>
        </el-collapse>
          </el-collapse-item>
        </el-collapse>
        <el-alert v-if="assetNotice" :title="assetNotice" type="info" :closable="false" show-icon />
        <div
          v-if="detailImageUrl && actionMapReady"
          class="state-image-stage"
          :style="{
            aspectRatio: `${detailDisplayWidth}/${detailDisplayHeight}`,
            width: `min(100%, ${620 * detailDisplayWidth / detailDisplayHeight}px)`,
          }"
        >
          <img :src="detailImageUrl" class="state-image-layer" alt="脱敏状态截图" />
          <svg class="action-map-overlay" preserveAspectRatio="xMidYMid meet" :viewBox="`0 0 ${actionMapWidth} ${actionMapHeight}`" aria-label="历史动作地图">
            <template v-for="(item, index) in actionMap.actions" :key="item.action_key || item.id || index">
              <g v-if="parseActionBounds(item)">
                <rect
                  :x="parseActionBounds(item)[0]"
                  :y="parseActionBounds(item)[1]"
                  :width="Math.max(1, parseActionBounds(item)[2] - parseActionBounds(item)[0])"
                  :height="Math.max(1, parseActionBounds(item)[3] - parseActionBounds(item)[1])"
                  fill="transparent"
                  :stroke="actionStroke(item)"
                  :stroke-dasharray="actionDash(item)"
                  stroke-width="4"
                />
                <g v-if="actionOrder(item)">
                  <circle
                    :cx="parseActionBounds(item)[0] + 14"
                    :cy="parseActionBounds(item)[1] + 14"
                    r="14"
                    :fill="actionStroke(item)"
                  />
                  <text
                    :x="parseActionBounds(item)[0] + 14"
                    :y="parseActionBounds(item)[1] + 21"
                    text-anchor="middle"
                    fill="#fff"
                    font-size="19"
                    font-weight="700"
                  >{{ actionOrder(item) }}</text>
                </g>
              </g>
            </template>
          </svg>
        </div>
        <img v-else-if="detailImageUrl" :src="detailImageUrl" class="state-image" alt="脱敏状态截图" />
        <section v-if="actionMap.actions.length" class="action-map-list">
          <div class="detail-section-title">历史动作地图</div>
          <el-table :data="actionMap.actions" size="small" max-height="260">
            <el-table-column label="#" width="48" align="center">
              <template #default="{ row, $index }">{{ actionOrder(row, $index + 1) }}</template>
            </el-table-column>
            <el-table-column label="动作" min-width="190">
              <template #default="{ row }">{{ actionMapLabel(row) }}</template>
            </el-table-column>
            <el-table-column label="结果" width="120">
              <template #default="{ row }">{{ actionStatusLabel(row) }}</template>
            </el-table-column>
          </el-table>
        </section>
        <el-button v-if="hasXmlEvidence && !xmlText" @click="loadXml">加载脱敏 XML</el-button>
        <pre v-if="xmlText" class="xml-view">{{ xmlText }}</pre>
      </div>
      <div v-else-if="selectedAction" class="drawer-content">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="操作" :span="2">{{ actionTypeLabel(selectedAction) }}“{{ actionTargetLabel(selectedAction) }}”</el-descriptions-item>
          <el-descriptions-item label="来源页面">{{ pageLabelForStateId(selectedAction.source || selectedAction.from_state_id) }}</el-descriptions-item>
          <el-descriptions-item label="目标页面">{{ selectedAction.target || selectedAction.to_state_id ? pageLabelForStateId(selectedAction.target || selectedAction.to_state_id) : '-' }}</el-descriptions-item>
          <el-descriptions-item label="执行结果">{{ actionStatusLabel(selectedAction) }}</el-descriptions-item>
          <el-descriptions-item label="耗时">{{ selectedAction.duration_ms ?? 0 }} ms</el-descriptions-item>
          <el-descriptions-item v-if="selectedAction.reason || selectedAction.error_message" label="说明" :span="2">{{ selectedAction.reason || selectedAction.error_message }}</el-descriptions-item>
        </el-descriptions>
        <el-collapse class="technical-details">
          <el-collapse-item title="技术信息" name="action-technical">
            <el-descriptions :column="2" border size="small">
              <el-descriptions-item label="Transition ID">{{ selectedAction.id || '-' }}</el-descriptions-item>
              <el-descriptions-item label="全局序号">#{{ selectedAction.sequence ?? '-' }}</el-descriptions-item>
              <el-descriptions-item label="动作类型">{{ selectedAction.action_type || '-' }}</el-descriptions-item>
              <el-descriptions-item label="定位方式">{{ selectedActionLocator }}</el-descriptions-item>
              <el-descriptions-item label="拓扑">{{ selectedAction.topology_type || '-' }}</el-descriptions-item>
              <el-descriptions-item label="遍历次数">{{ selectedAction.traversal_count || 1 }}</el-descriptions-item>
              <el-descriptions-item label="风险">{{ selectedAction.risk_type || '-' }}</el-descriptions-item>
              <el-descriptions-item label="动作角色">{{ selectedAction.action_role_key || '-' }}</el-descriptions-item>
              <el-descriptions-item label="执行方式">{{ inspectionExecutionDispositionLabel(selectedAction.execution_disposition) }}</el-descriptions-item>
              <el-descriptions-item label="失败分类">{{ selectedAction.failure_type || '-' }}</el-descriptions-item>
              <el-descriptions-item label="Contract ID">{{ selectedAction.coverage_contract_id || '-' }}</el-descriptions-item>
              <el-descriptions-item label="复用 Transition ID">{{ selectedAction.coverage_source_transition_id || '-' }}</el-descriptions-item>
              <el-descriptions-item label="恢复次数">{{ selectedAction.recovery_retry_count ?? '-' }}</el-descriptions-item>
              <el-descriptions-item label="来源 Observation ID">
                <el-button v-if="selectedAction.source_observation_id" link type="primary" @click="openTransitionObservation('source')">{{ selectedAction.source_observation_id }}</el-button>
                <span v-else>-</span>
              </el-descriptions-item>
              <el-descriptions-item label="目标 Observation ID">
                <el-button v-if="selectedAction.target_observation_id" link type="primary" @click="openTransitionObservation('target')">{{ selectedAction.target_observation_id }}</el-button>
                <span v-else>-</span>
              </el-descriptions-item>
            </el-descriptions>
          </el-collapse-item>
        </el-collapse>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.inspection-report { height: 100%; display: flex; flex-direction: column; background: #f5f7fa; overflow: hidden; }
.toolbar { padding: 12px 16px; display: flex; align-items: center; justify-content: space-between; gap: 16px; background: #fff; border-bottom: 1px solid #ebeef5; }
.toolbar-left, .toolbar-right, .filters, .selection-header { display: flex; align-items: center; gap: 12px; }
.title { font-size: 17px; font-weight: 700; color: #303133; }
.subtitle { font-size: 12px; color: #909399; }
.stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1px; background: #ebeef5; border-bottom: 1px solid #ebeef5; }
.stats > div { min-width: 0; padding: 11px 16px; display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; background: #fff; }
.stats span { color: #909399; font-size: 12px; }
.stats strong { font-size: 20px; }
.stats small { grid-column: 1 / -1; margin-top: 3px; overflow: hidden; color: #909399; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.metric-label { display: inline-flex; align-items: center; gap: 4px; }
.metric-label :deep(.el-icon) { color: #909399; cursor: help; }
.running-summary { min-height: 42px; padding: 8px 16px; display: flex; align-items: center; gap: 10px 22px; overflow: hidden; border-bottom: 1px solid #ebeef5; background: #fff; color: #606266; font-size: 12px; }
.running-summary strong { flex: none; color: #303133; }
.running-summary span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.coverage-blind-spots { flex: none; min-height: 36px; padding: 7px 16px; display: flex; align-items: center; gap: 8px 18px; flex-wrap: wrap; border-bottom: 1px solid #fab6b6; background: #fef0f0; color: #c45656; font-size: 12px; }
.coverage-blind-spots strong { color: #b42318; }
.diagnostic-collapse { flex: none; border-bottom: 1px solid #ebeef5; background: #fff; }
.diagnostic-collapse :deep(.el-collapse-item__header) { height: 34px; padding: 0 16px; color: #606266; font-size: 12px; }
.diagnostic-collapse :deep(.el-collapse-item__wrap) { border-bottom: 0; }
.diagnostic-collapse :deep(.el-collapse-item__content) { max-height: 190px; overflow: auto; padding: 0 16px 10px; }
.diagnostics-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 260px), 1fr)); gap: 8px 22px; color: #606266; font-size: 12px; }
.diagnostics-grid span { min-width: 0; display: grid; grid-template-columns: max-content minmax(0, 1fr); align-items: start; gap: 10px; }
.diagnostics-grid em { color: #909399; font-style: normal; white-space: nowrap; }
.diagnostics-grid strong { min-width: 0; color: #606266; font-weight: 500; line-height: 1.5; overflow-wrap: anywhere; text-align: right; white-space: normal; }
.success { color: #67c23a; } .warning { color: #e6a23c; } .danger { color: #f56c6c; }
.report-tabs { flex: 1; min-height: 0; margin: 12px; padding: 0 14px 14px; background: #fff; border: 1px solid #ebeef5; border-radius: 4px; }
.report-tabs :deep(.el-tabs__content), .report-tabs :deep(.el-tab-pane) { height: calc(100% - 28px); }
.coverage-tab { height: 100%; overflow: auto; }
.coverage-manifest-line { min-height: 42px; display: flex; align-items: center; gap: 10px; border-bottom: 1px solid #ebeef5; color: #606266; font-size: 12px; }
.coverage-manifest-line code { color: #909399; }
.coverage-branch { padding: 12px 0 18px; border-bottom: 1px solid #ebeef5; }
.surface-gap { display: flex; flex-direction: column; gap: 10px; }
.surface-gap-note { margin: 0; color: #909399; font-size: 12px; }
.coverage-branch:last-child { border-bottom: 0; }
.coverage-branch header { min-height: 32px; display: flex; align-items: center; gap: 10px; }
.coverage-branch h3 { margin: 0; color: #303133; font-size: 14px; letter-spacing: 0; }
.coverage-branch header > span { color: #909399; font-size: 12px; }
.coverage-branch :deep(.el-table) { width: 100%; }
.coverage-branch :deep(.el-table .cell) { white-space: normal; overflow-wrap: anywhere; }
.coverage-evidence-links { display: flex; align-items: flex-start; gap: 2px 10px; flex-direction: column; }
.coverage-evidence-group { display: flex; min-width: 0; align-items: center; gap: 2px 8px; flex-wrap: wrap; }
.coverage-evidence-group em { color: #909399; font-size: 11px; font-style: normal; }
.coverage-evidence-group code { color: #606266; font-size: 11px; }
.coverage-evidence-links :deep(.el-button) { height: 24px; margin: 0; padding: 0; }
.filters { min-height: 46px; padding: 6px 0; flex-wrap: wrap; }
.tree-legend { display: flex; align-items: center; gap: 10px; color: #606266; font-size: 11px; white-space: nowrap; }
.tree-legend span { display: inline-flex; align-items: center; gap: 4px; }
.legend-dot { width: 9px; height: 9px; border: 2px solid #c0c4cc; border-radius: 3px; background: #fff; }
.legend-dot.is-home { border-color: #409eff; background: #ecf5ff; }
.legend-dot.is-page { border-color: #67c23a; background: #f0f9eb; }
.legend-dot.is-viewport { width: 13px; border-style: dotted; border-color: #67c23a; background: #f0f9eb; }
.legend-dot.is-reference { border-style: dashed; border-radius: 50%; border-color: #909399; }
.mind-map-summary { margin-left: auto; white-space: nowrap; }
.mind-map-shell { position: relative; height: calc(100% - 58px); min-height: 0; overflow: hidden; border: 1px solid #ebeef5; background: #fff; }
.mind-map-floating-tools { position: absolute; z-index: 5; top: 10px; right: 12px; display: flex; align-items: center; gap: 4px; padding: 5px; border: 1px solid #dcdfe6; border-radius: 6px; background: rgba(255, 255, 255, 0.96); box-shadow: 0 4px 14px rgba(31, 35, 41, 0.12); }
.mind-map-search { width: 210px; margin-right: 4px; }
.mind-map-zoom-value { width: 46px; color: #606266; font-variant-numeric: tabular-nums; font-size: 12px; text-align: center; }
.mind-map-viewport { width: 100%; height: 100%; min-height: 0; overflow: auto; cursor: grab; overscroll-behavior: contain; }
.mind-map-viewport.is-dragging { cursor: grabbing; user-select: none; }
.mind-map-world { position: relative; min-width: 1px; min-height: 1px; }
.mind-map-chart { position: absolute; top: 0; left: 0; min-height: 0; transform-origin: 0 0; }
.selection-header { margin-bottom: 14px; }
.selection-header :deep(.el-alert) { flex: 1; }
.state-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px; overflow: auto; max-height: calc(100% - 60px); }
.state-grid :deep(.el-checkbox) { width: 100%; height: auto; padding: 12px; margin: 0; }
.state-grid small { display: block; margin-top: 4px; color: #909399; }
.drawer-content { display: flex; flex-direction: column; gap: 14px; }
.technical-details { border-top: 1px solid #ebeef5; }
.technical-details :deep(.el-collapse-item__header) { height: 34px; color: #606266; font-size: 13px; }
.observation-section { border-top: 1px solid #ebeef5; padding-top: 12px; }
.observation-section-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.observation-timeline { max-height: 290px; margin: 10px 0 0; padding-right: 4px; overflow: auto; }
.observation-entry { width: 100%; padding: 8px 10px; border: 1px solid #ebeef5; border-radius: 4px; background: #fff; color: #606266; text-align: left; cursor: pointer; }
.observation-entry:hover, .observation-entry.is-active { border-color: #409eff; background: #ecf5ff; }
.observation-title, .observation-tags { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.observation-title { color: #303133; font-weight: 600; }
.observation-tags { margin-top: 6px; color: #909399; font-size: 12px; }
.state-image { width: 100%; max-height: 620px; object-fit: contain; border: 1px solid #ebeef5; background: #f5f7fa; }
.state-image-stage { position: relative; max-height: 620px; margin: 0 auto; overflow: hidden; border: 1px solid #ebeef5; background: #f5f7fa; }
.state-image-layer, .action-map-overlay { position: absolute; inset: 0; width: 100%; height: 100%; }
.state-image-layer { object-fit: contain; }
.action-map-overlay { pointer-events: none; }
.action-map-list { display: flex; flex-direction: column; gap: 8px; }
.detail-section-title { color: #303133; font-size: 14px; font-weight: 600; }
.xml-view { max-height: 440px; overflow: auto; padding: 12px; font-size: 11px; line-height: 1.5; white-space: pre-wrap; word-break: break-all; background: #1f2329; color: #d8dee9; border-radius: 4px; }

@media (max-height: 650px) and (min-width: 900px) {
  .inspection-report { overflow-y: auto; }
  .report-tabs { flex: none; height: 410px; min-height: 410px; }
  .report-tabs :deep(.el-tabs__content),
  .report-tabs :deep(.el-tab-pane) { height: calc(100% - 28px); overflow: hidden; }
  .mind-map-shell { height: calc(100% - 58px); min-height: 300px; }
  .mind-map-viewport { height: 100%; min-height: 300px; }
}

@media (max-width: 560px) {
  .diagnostics-grid { grid-template-columns: minmax(0, 1fr); }
}

@media (max-width: 899px) {
  .inspection-report { overflow: auto; }
  .toolbar { align-items: flex-start; flex-direction: column; }
  .toolbar-left { width: 100%; flex-wrap: wrap; }
  .toolbar-right { width: 100%; justify-content: flex-end; }
  .stats { grid-template-columns: repeat(2, 1fr); }
  .report-tabs { flex: none; min-height: 760px; }
  .report-tabs :deep(.el-tabs__content), .report-tabs :deep(.el-tab-pane) { height: auto; min-height: 680px; overflow: visible; }
  .filters { height: auto; min-height: 112px; padding: 8px 0; flex-wrap: wrap; align-content: center; }
  .tree-legend { order: 5; width: 100%; flex-wrap: wrap; }
  .mind-map-summary { margin-left: 0; }
  .mind-map-shell, .mind-map-viewport { height: 680px; min-height: 680px; }
  :deep(.el-drawer) { max-width: 100vw; }
}

@media (max-width: 560px) {
  .toolbar-right { justify-content: flex-start; flex-wrap: wrap; }
  .stats { grid-template-columns: minmax(0, 1fr); }
  .stats > div { padding: 10px 12px; }
  .report-tabs { margin: 8px; padding-inline: 8px; }
  .filters :deep(.el-select) { width: 100% !important; }
  .mind-map-floating-tools { right: 6px; left: 6px; overflow-x: auto; }
  .mind-map-search { min-width: 150px; flex: 1; }
}
</style>
