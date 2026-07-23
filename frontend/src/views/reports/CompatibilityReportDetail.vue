<script setup>
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import dayjs from 'dayjs'
import api from '@/api'
import { runStatusTagType } from '@/utils/statusMeta'
import {
  compatibilityStatusLabel,
  compatibilityExecutionMode,
  normalizeReplayResults,
  normalizeReplayTrace,
  packageSnapshotLabel,
  replayBoundaryEvidenceLabel,
  replayFailureLabel,
  replayPathLabel,
  replayScopeLabel,
  sourceBoundaryEvidenceLabel,
  terminalOutcomeLabel,
} from '@/utils/compatibilityReplay'

const route = useRoute()
const router = useRouter()
const run = ref(null)
const devices = ref([])
const selectedResult = ref(null)
const resultDrawerVisible = ref(false)
const loading = ref(false)
const assetObjectUrls = ref({})
const legacyAssetFallbacks = ref({})
const assetNotices = ref({})
const generatedDiffAssetIds = ref({})
const generatedDiffLoading = ref({})
const generatedDiffNotices = ref({})
const assetRequests = new Set()
const generatedDiffRequests = new Set()
const ownedObjectUrls = new Set()
let pollTimer = null
let pageActive = false
let assetLifecycleVersion = 0

const matrixPages = computed(() => run.value?.page_set?.pages || [])
const isInstalledReplay = computed(() => compatibilityExecutionMode(run.value) === 'installed_replay')
const isDeviceCompare = computed(() => run.value?.compare_mode === 'device')
const replayResults = computed(() => normalizeReplayResults(run.value))
const replaySourcePackage = computed(() => run.value?.source_package_snapshot || {})
const replayTargetPackage = computed(() => (
  run.value?.target_package_snapshot
  || run.value?.cells?.[0]?.installed_package_snapshot
  || {}
))
const sourceVersionUnknown = computed(() => (
  run.value && isInstalledReplay.value && replaySourcePackage.value.known === false
))
const isUnknownSourceWarning = item => {
  const code = String(item?.code || item?.type || '').trim().toUpperCase()
  const message = String(item?.message || item || '')
  return ['SOURCE_VERSION_UNKNOWN', 'SOURCE_PACKAGE_UNKNOWN'].includes(code)
    || (message.includes('历史巡检') && message.includes('来源版本'))
}
const replayPackageWarnings = computed(() => (
  (replayTargetPackage.value?.preflight_warnings || []).filter(item => (
    !(sourceVersionUnknown.value && isUnknownSourceWarning(item))
  ))
))
const replayDeviceSerial = computed(() => (
  run.value?.cells?.[0]?.device_serial || replayResults.value[0]?.device_serial || '-'
))
const selectedReplayTrace = computed(() => {
  const trace = selectedResult.value?.replay_trace
  return Array.isArray(trace) ? trace : normalizeReplayTrace(trace)
})
const hasRunningRun = computed(() => {
  const status = String(run.value?.status || '').toUpperCase()
  return ['PENDING', 'RUNNING'].includes(status)
})
const galleryPageKey = ref('')
const galleryPage = computed(() => (
  matrixPages.value.find(page => (page.key || page.name) === galleryPageKey.value) || null
))
const galleryCards = computed(() => {
  if (!galleryPage.value || !run.value?.cells?.length) return []
  const cards = run.value.cells.map(cell => ({
    cell,
    result: findPageResult(cell, galleryPage.value) || null,
  }))
  return cards.sort((a, b) => Number(b.cell.is_baseline || false) - Number(a.cell.is_baseline || false))
})

watch(matrixPages, (pages) => {
  if (!pages.length) {
    galleryPageKey.value = ''
    return
  }
  if (!pages.some(page => (page.key || page.name) === galleryPageKey.value)) {
    galleryPageKey.value = pages[0].key || pages[0].name
  }
}, { immediate: true })

const formatTime = (value) => value ? dayjs(value).format('MM-DD HH:mm:ss') : '-'
const statusText = (status) => String(status || 'PENDING').toUpperCase()
const statusLabel = status => compatibilityStatusLabel(status)
const statusType = (status) => runStatusTagType(statusText(status))
const statusColor = (status) => ({
  PASS: '#67C23A',
  WARNING: '#E6A23C',
  FAIL: '#F56C6C',
  ERROR: '#F56C6C',
}[statusText(status)] || '#dcdfe6')
const metricLabels = {
  pixel_diff_ratio: '像素差异比例',
  ssim: '结构相似度',
  visual_similarity: '视觉相似度',
  xml_diff_ratio: 'XML 差异比例',
  size_changed: '截图尺寸变化',
  has_crash_or_anr: 'Crash/ANR',
  required_text_missing: '必需文本缺失',
  same_resolution: '与基准同分辨率',
  activity_mismatch: 'Activity 不一致',
  resolution: '分辨率',
  baseline_device_serial: '基准设备',
  is_baseline: '基准设备行',
}
const hiddenMetricKeys = new Set([
  'ocr_diff_ratio',
  'ocr_error_baseline',
  'ocr_error_candidate',
  'duration_ms',
  'checkpoint_count',
  'completed_checkpoints',
  'warning_codes',
  'reachability_evidence',
  'replay_eligibility',
  'replay_scope',
  'terminal_outcome',
  'boundary_evidence',
  'source_boundary_evidence',
  'replay_boundary_evidence',
  'execution_boundary_evidence',
  'prefix_path_key',
  'terminal_boundaries',
  'asset_error',
])
const formatMode = (mode) => mode === 'clean' ? '干净对比' : '升级兼容'
const formatCompareMode = (mode) => ({
  snapshot: '快照回归',
  version: '版本对比',
  device: '机型对比',
}[mode] || '兼容性对比')
const formatSourceType = (sourceType) => (
  sourceType === 'inspection' ? '智能巡检状态' : '人工页面合集'
)
const evidenceLabel = value => ({
  VERIFIED_TWICE: '已验证两次',
  OBSERVED_ONCE: '已观测一次',
  UNSTABLE: '历史链路不稳定',
  UNKNOWN: '证据待确认',
}[String(value || '').toUpperCase()] || '证据待确认')
const replayStepType = status => ({
  PASS: 'success',
  WARNING: 'warning',
  BLOCKED: 'warning',
  FAIL: 'danger',
  ERROR: 'danger',
}[String(status || '').toUpperCase()] || 'info')
const formatDuration = value => {
  if (value === null || value === undefined || value === '') return '-'
  const number = Number(value)
  if (!Number.isFinite(number)) return '-'
  if (number < 1000) return `${number} ms`
  return `${(number / 1000).toFixed(1)} s`
}
const branchLabel = value => ({
  guest: '未登录',
  authenticated: '已登录',
}[String(value || '').toLowerCase()] || '默认业务线')
const stageLabel = value => ({
  PENDING: '等待执行',
  PREFLIGHT: '检查设备',
  REPLAYING: '回放链路',
  CAPTURING: '保存证据',
  CANCELLING: '正在取消',
  CANCELLED: '已取消',
  FINISHED: '已完成',
  COMPLETED: '已完成',
}[String(value || '').trim().toUpperCase()] || (hasRunningRun.value ? '执行中' : '已完成'))
const replayFailureText = result => {
  const terminalOutcome = String(result?.terminal_outcome || '').toUpperCase()
  const failureCode = result?.failure_type || (terminalOutcome !== 'NONE' ? terminalOutcome : '')
  return replayFailureLabel(failureCode)
    || (statusText(result?.status) === 'PASS' ? '链路可正常到达' : statusLabel(result?.status))
}
const hasSafetyBoundary = result => (
  String(result?.terminal_outcome || '').toUpperCase() === 'SAFETY_BLOCKED'
  || String(result?.replay_scope || '').toUpperCase() === 'PREFIX_TO_SAFETY_BOUNDARY'
)
const formatMetricLabel = (key) => metricLabels[key] || key
const formatMetricValue = (value) => {
  if (typeof value === 'boolean') return value ? '是' : '否'
  return value ?? '-'
}
const displayMetrics = (metrics = {}) => (
  Object.entries(metrics)
    .filter(([key]) => !hiddenMetricKeys.has(key))
    .map(([key, value]) => ({ key, value }))
)
const assetIdFor = (result, kind) => {
  if (kind === 'diff_screenshot' && result?.id) {
    return result.diff_screenshot_asset_id || generatedDiffAssetIds.value[result.id] || ''
  }
  return result?.[`${kind}_asset_id`] || ''
}
const assetPathFor = (result, kind) => result?.[`${kind}_path`] || ''
const legacyAssetUrl = path => path ? api.getReportAssetUrl(path) : ''
const resultAssetUrl = (result, kind) => {
  const assetId = assetIdFor(result, kind)
  return assetId
    ? assetObjectUrls.value[assetId] || legacyAssetFallbacks.value[assetId] || ''
    : legacyAssetUrl(assetPathFor(result, kind))
}
const hasResultAsset = (result, kind) => Boolean(assetIdFor(result, kind) || assetPathFor(result, kind))
const resultAssetNotice = (result, kind) => assetNotices.value[assetIdFor(result, kind)] || ''
const diffPlaceholder = result => {
  if (generatedDiffLoading.value[result?.id]) return '正在生成差异图'
  if (generatedDiffNotices.value[result?.id]) return generatedDiffNotices.value[result.id]
  if (statusText(result?.status) === 'PASS') return '通过结果的差异图按需生成'
  return isDeviceCompare.value ? '跨分辨率不生成差异图' : '暂无差异图'
}

const releaseAssetUrls = () => {
  assetLifecycleVersion += 1
  ownedObjectUrls.forEach(url => URL.revokeObjectURL(url))
  ownedObjectUrls.clear()
  assetRequests.clear()
  generatedDiffRequests.clear()
  assetObjectUrls.value = {}
  legacyAssetFallbacks.value = {}
  assetNotices.value = {}
  generatedDiffAssetIds.value = {}
  generatedDiffLoading.value = {}
  generatedDiffNotices.value = {}
}

const hydrateAsset = async (assetId, legacyPath = '') => {
  if (
    !assetId
    || assetObjectUrls.value[assetId]
    || legacyAssetFallbacks.value[assetId]
    || assetRequests.has(assetId)
  ) return
  const requestVersion = assetLifecycleVersion
  assetRequests.add(assetId)
  try {
    const response = await api.getAsset(assetId, 'blob')
    if (requestVersion !== assetLifecycleVersion) return
    const url = URL.createObjectURL(response.data)
    ownedObjectUrls.add(url)
    assetObjectUrls.value = { ...assetObjectUrls.value, [assetId]: url }
  } catch (error) {
    if (requestVersion !== assetLifecycleVersion) return
    if ([404, 410].includes(Number(error.response?.status)) && legacyPath) {
      legacyAssetFallbacks.value = {
        ...legacyAssetFallbacks.value,
        [assetId]: legacyAssetUrl(legacyPath),
      }
      return
    }
    const message = error.response?.status === 410
      ? '资产已按保留策略清理'
      : error.response?.data?.detail || error.message || '资产加载失败'
    assetNotices.value = { ...assetNotices.value, [assetId]: message }
  } finally {
    if (requestVersion === assetLifecycleVersion) assetRequests.delete(assetId)
  }
}

const hydrateResultAssets = (result, includeDiff = true) => {
  if (!result) return
  const kinds = ['baseline_screenshot', 'candidate_screenshot']
  if (includeDiff) kinds.push('diff_screenshot')
  kinds.forEach(kind => {
    void hydrateAsset(assetIdFor(result, kind), assetPathFor(result, kind))
  })
}

const hydrateOnDemandDiff = async result => {
  const resultId = Number(result?.id)
  const runId = Number(run.value?.id)
  if (
    !resultId
    || !runId
    || statusText(result.status) !== 'PASS'
    || hasResultAsset(result, 'diff_screenshot')
    || generatedDiffRequests.has(resultId)
  ) return

  const requestVersion = assetLifecycleVersion
  generatedDiffRequests.add(resultId)
  generatedDiffLoading.value = { ...generatedDiffLoading.value, [resultId]: true }
  const notices = { ...generatedDiffNotices.value }
  delete notices[resultId]
  generatedDiffNotices.value = notices
  try {
    const { data } = await api.getCompatibilityPageDiff(runId, resultId)
    if (requestVersion !== assetLifecycleVersion) return
    const assetId = data?.asset_id
    if (!assetId) throw new Error('服务未返回差异图资产')
    generatedDiffAssetIds.value = { ...generatedDiffAssetIds.value, [resultId]: assetId }
    await hydrateAsset(assetId)
  } catch (error) {
    if (requestVersion !== assetLifecycleVersion) return
    generatedDiffNotices.value = {
      ...generatedDiffNotices.value,
      [resultId]: error.response?.status === 410
        ? '生成差异图所需截图已按保留策略清理'
        : error.response?.data?.detail || error.message || '差异图生成失败',
    }
  } finally {
    if (requestVersion === assetLifecycleVersion) {
      generatedDiffRequests.delete(resultId)
      generatedDiffLoading.value = { ...generatedDiffLoading.value, [resultId]: false }
    }
  }
}
const formatDevice = (serial) => {
  const device = devices.value.find(item => item.serial === serial)
  return device?.custom_name || device?.market_name || device?.model || serial || '-'
}

const fetchDevices = async () => {
  const { data } = await api.getDeviceList()
  devices.value = data || []
}
const fetchRun = async () => {
  const id = Number(route.params.id)
  if (!id) return
  loading.value = true
  try {
    const { data } = await api.getCompatibilityRun(id)
    run.value = data
  } catch (err) {
    ElMessage.error(err.response?.data?.detail || err.message || '加载兼容性报告失败')
  } finally {
    loading.value = false
  }
}
const refreshAll = async () => {
  await Promise.all([fetchDevices(), fetchRun()])
}

const findPageResult = (cell, page) => (
  (cell.pages || []).find(item => item.page_key === page.key || item.page_name === page.name)
)
const openResult = (cell, page) => {
  const result = findPageResult(cell, page)
  if (!result) return
  selectedResult.value = { ...result, device_serial: cell.device_serial }
  resultDrawerVisible.value = true
  hydrateResultAssets(selectedResult.value)
  void hydrateOnDemandDiff(selectedResult.value)
}
const openReplayResult = result => {
  if (!result) return
  selectedResult.value = result
  resultDrawerVisible.value = true
  void hydrateAsset(
    assetIdFor(result, 'candidate_screenshot'),
    assetPathFor(result, 'candidate_screenshot'),
  )
}

const openXmlAsset = async (assetId, path) => {
  try {
    const response = assetId
      ? await api.getAsset(assetId, 'text')
      : await api.getReportAssetUrl(path)
    if (!assetId) {
      window.open(response, '_blank', 'noopener')
      return
    }
    const blob = new Blob([String(response.data || '')], { type: 'text/xml;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    ownedObjectUrls.add(url)
    window.open(url, '_blank', 'noopener')
  } catch (error) {
    if (assetId && path && [404, 410].includes(Number(error.response?.status))) {
      window.open(legacyAssetUrl(path), '_blank', 'noopener')
      return
    }
    ElMessage.error(error.response?.status === 410 ? 'XML 已按保留策略清理' : error.response?.data?.detail || error.message || 'XML 加载失败')
  }
}
const cancelRun = async () => {
  if (!run.value?.id) return
  await api.cancelCompatibilityRun(run.value.id)
  ElMessage.success('已发送取消请求')
  await fetchRun()
}
const goBack = () => {
  router.push('/execution/reports?tab=compatibility')
}

const startPolling = () => {
  if (pollTimer) return
  pollTimer = setInterval(() => {
    if (hasRunningRun.value) fetchRun()
  }, 8000)
}
const stopPolling = () => {
  if (!pollTimer) return
  clearInterval(pollTimer)
  pollTimer = null
}
const activatePage = () => {
  if (pageActive) return
  pageActive = true
  startPolling()
}
const deactivatePage = () => {
  pageActive = false
  stopPolling()
}

watch(() => route.params.id, () => {
  releaseAssetUrls()
  refreshAll()
})

watch(
  () => galleryCards.value.map(card => assetIdFor(card.result, 'candidate_screenshot')).filter(Boolean).join(','),
  () => galleryCards.value.slice(0, 32).forEach(card => hydrateResultAssets(card.result, false)),
  { immediate: true },
)

onMounted(async () => {
  await refreshAll()
  activatePage()
})
onActivated(() => {
  refreshAll()
  activatePage()
})
onDeactivated(deactivatePage)
onUnmounted(() => {
  deactivatePage()
  releaseAssetUrls()
})
</script>

<template>
  <div class="compat-report-detail" v-loading="loading">
    <div class="content-wrapper">
      <div class="detail-header">
        <div class="title-block">
          <el-button :icon="ArrowLeft" link type="primary" @click="goBack">返回报告中心</el-button>
          <h2>{{ run?.name || '兼容性报告' }}</h2>
          <p>{{ run?.package_name || '-' }}</p>
        </div>
        <div class="header-actions">
          <el-button v-if="hasRunningRun" type="danger" plain @click="cancelRun">取消任务</el-button>
          <el-button :icon="Refresh" @click="refreshAll">刷新</el-button>
        </div>
      </div>

      <el-alert
        v-if="sourceVersionUnknown"
        title="巡检来源版本未知，本次结果仅证明当前已安装版本的历史链路可达性"
        type="warning"
        :closable="false"
        show-icon
      />
      <el-alert
        v-for="item in replayPackageWarnings"
        :key="`replay-warning-${item.code || item.message}`"
        :title="item.message || item.code || item"
        type="warning"
        :closable="false"
        show-icon
      />

      <div v-if="run && isInstalledReplay" class="kpi-row">
        <div class="kpi-item">
          <span class="kpi-label">状态</span>
          <el-tag :type="statusType(run.status)" effect="plain">{{ statusLabel(run.status) }}</el-tag>
        </div>
        <div class="kpi-item version-kpi">
          <span class="kpi-label">版本</span>
          <strong>{{ packageSnapshotLabel(replaySourcePackage) }} → {{ packageSnapshotLabel(replayTargetPackage) }}</strong>
        </div>
        <div class="kpi-item"><span class="kpi-label">执行环境</span><strong>{{ formatDevice(replayDeviceSerial) }} · {{ branchLabel(run.replay_branch_key) }}</strong></div>
        <div class="kpi-item">
          <span class="kpi-label">链路结果</span>
          <strong><span class="pass">{{ run.pass_count || 0 }} 通过</span> · <span class="warn">{{ run.warning_count || 0 }} 需关注</span> · <span class="fail">{{ run.fail_count || 0 }} 失败</span></strong>
        </div>
      </div>

      <div v-else-if="run" class="kpi-row">
        <div class="kpi-item">
          <span class="kpi-label">状态</span>
          <el-tag :type="statusType(run.status)" effect="plain">{{ statusLabel(run.status) }}</el-tag>
        </div>
        <div class="kpi-item"><span class="kpi-label">页面来源</span><strong>{{ formatSourceType(run.source_type) }}</strong></div>
        <div class="kpi-item"><span class="kpi-label">对比维度</span><strong>{{ formatCompareMode(run.compare_mode) }}</strong></div>
        <div class="kpi-item"><span class="kpi-label">模式</span><strong>{{ formatMode(run.mode) }}</strong></div>
        <div v-if="isDeviceCompare" class="kpi-item"><span class="kpi-label">基准设备</span><strong>{{ formatDevice(run.baseline_device_serial) }}</strong></div>
        <div class="kpi-item"><span class="kpi-label">设备</span><strong>{{ run.total_cells }}</strong></div>
        <div class="kpi-item"><span class="kpi-label">页面</span><strong>{{ matrixPages.length }}</strong></div>
        <div class="kpi-item"><span class="kpi-label">通过</span><strong class="pass">{{ run.pass_count }}</strong></div>
        <div class="kpi-item"><span class="kpi-label">警告</span><strong class="warn">{{ run.warning_count }}</strong></div>
        <div class="kpi-item"><span class="kpi-label">失败</span><strong class="fail">{{ run.fail_count }}</strong></div>
        <div class="kpi-item"><span class="kpi-label">开始</span><span>{{ formatTime(run.started_at || run.created_at) }}</span></div>
      </div>

      <section v-if="run && isInstalledReplay" class="replay-panel">
        <div class="replay-stage">
          <div>
            <strong>链路回放结果</strong>
            <span class="muted-text">每条链路独立回根执行；失败不会阻断后续链路</span>
          </div>
          <div v-if="run.cells?.[0]" class="stage-text">
              {{ stageLabel(run.cells[0].current_stage) }}
              <el-tag :type="statusType(run.cells[0].status)" size="small" effect="plain">{{ statusLabel(run.cells[0].status) }}</el-tag>
            </div>
          </div>
          <el-table :data="replayResults" border height="100%" empty-text="等待生成链路结果">
          <el-table-column label="链路" min-width="310">
            <template #default="{ row }">
              <div class="run-name">{{ row.name }}</div>
              <div class="muted-text replay-path-label">{{ replayPathLabel(row) }}</div>
            </template>
          </el-table-column>
          <el-table-column label="来源页面" min-width="190">
            <template #default="{ row }">
              <div>{{ row.source_reference }}</div>
              <div class="muted-text">{{ row.source_capture_label }} · {{ evidenceLabel(row.evidence_level) }}</div>
            </template>
          </el-table-column>
          <el-table-column label="回放范围" min-width="190">
            <template #default="{ row }">
              <div>{{ replayScopeLabel(row.replay_scope) }}</div>
              <div class="muted-text">{{ terminalOutcomeLabel(row.terminal_outcome) }}</div>
              <div v-if="hasSafetyBoundary(row)" class="boundary-inline muted-text">
                <span>{{ sourceBoundaryEvidenceLabel(row.source_boundary_evidence) }}</span>
                <span>{{ replayBoundaryEvidenceLabel(row.replay_boundary_evidence) }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="本次结果" min-width="170">
            <template #default="{ row }">
              <el-tag :type="statusType(row.status)" size="small" effect="plain">{{ statusLabel(row.status) }}</el-tag>
              <div class="muted-text result-summary">{{ replayFailureText(row) }}<span v-if="row.failed_step_index !== null"> · 第 {{ Number(row.failed_step_index) + 1 }} 步</span></div>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="100" align="center">
            <template #default="{ row }">{{ formatDuration(row.duration_ms) }}</template>
          </el-table-column>
          <el-table-column label="详情" width="80" align="center" fixed="right">
            <template #default="{ row }"><el-button link type="primary" @click="openReplayResult(row)">查看</el-button></template>
          </el-table-column>
        </el-table>
      </section>

      <section v-else-if="run" class="matrix-panel">
        <el-table :data="run.cells || []" border class="matrix-table" height="100%">
          <el-table-column label="设备" fixed min-width="180">
            <template #default="{ row }">
              <div class="run-name">
                {{ formatDevice(row.device_serial) }}
                <el-tag v-if="row.is_baseline" size="small" type="info" effect="plain">基准</el-tag>
              </div>
              <div class="muted-text">{{ row.os_version || '-' }} / {{ row.resolution || '-' }}</div>
            </template>
          </el-table-column>
          <el-table-column label="阶段" min-width="160">
            <template #default="{ row }">
              <el-tag :type="statusType(row.status)" size="small" effect="plain">{{ statusLabel(row.status) }}</el-tag>
              <span class="stage-text">{{ row.current_stage || '-' }}</span>
            </template>
          </el-table-column>
          <el-table-column
            v-for="page in matrixPages"
            :key="page.key || page.name"
            :label="page.name"
            min-width="128"
            align="center"
          >
            <template #default="{ row }">
              <el-button
                v-if="findPageResult(row, page)"
                link
                :type="statusType(findPageResult(row, page).status)"
                @click.stop="openResult(row, page)"
              >
                {{ statusLabel(findPageResult(row, page).status) }}
              </el-button>
              <span v-else class="muted-text">-</span>
            </template>
          </el-table-column>
        </el-table>
      </section>

      <section v-if="run && !isInstalledReplay && galleryCards.length" class="gallery-panel">
        <div class="gallery-header">
          <span class="card-title">页面画廊</span>
          <el-select v-model="galleryPageKey" filterable class="gallery-page-select" size="small">
            <el-option
              v-for="page in matrixPages"
              :key="page.key || page.name"
              :label="page.name"
              :value="page.key || page.name"
            />
          </el-select>
          <span class="muted-text">同一页面各设备截图横向对照{{ isDeviceCompare ? '（基准设备置首）' : '' }}</span>
        </div>
        <div class="gallery-row">
          <div
            v-for="card in galleryCards"
            :key="card.cell.id"
            class="gallery-card"
            :style="{ borderColor: statusColor(card.result?.status) }"
            @click="card.result && openResult(card.cell, galleryPage)"
          >
            <div class="gallery-caption">
              <span class="gallery-device">
                {{ formatDevice(card.cell.device_serial) }}
                <el-tag v-if="card.cell.is_baseline" size="small" type="info" effect="plain">基准</el-tag>
              </span>
              <el-tag v-if="card.result" :type="statusType(card.result.status)" size="small" effect="plain">
                {{ statusLabel(card.result.status) }}
              </el-tag>
            </div>
            <div class="gallery-meta muted-text">{{ card.cell.resolution || '-' }}</div>
            <img
              v-if="resultAssetUrl(card.result, 'candidate_screenshot')"
              :src="resultAssetUrl(card.result, 'candidate_screenshot')"
              :alt="card.cell.device_serial"
              loading="lazy"
            />
            <div v-else class="gallery-empty muted-text">{{ resultAssetNotice(card.result, 'candidate_screenshot') || (hasResultAsset(card.result, 'candidate_screenshot') ? '加载中' : '暂无截图') }}</div>
          </div>
        </div>
      </section>

      <div v-else-if="!run" class="empty-state">暂无兼容性报告详情</div>
    </div>

    <el-drawer v-model="resultDrawerVisible" size="64%" :title="isInstalledReplay ? '链路回放详情' : '页面对比详情'">
      <div v-if="selectedResult && isInstalledReplay" class="result-detail replay-result-detail">
        <div class="result-header">
          <div>
            <h3>{{ selectedResult.name || '链路回放' }}</h3>
            <p class="muted-text">{{ selectedResult.source_reference }} · {{ selectedResult.source_capture_label }} · {{ formatDevice(selectedResult.device_serial) }}</p>
          </div>
          <el-tag :type="statusType(selectedResult.status)" effect="plain">{{ statusLabel(selectedResult.status) }}</el-tag>
        </div>
        <el-alert v-if="selectedResult.failure_type" :title="replayFailureText(selectedResult)" type="warning" show-icon :closable="false" />
        <div class="metric-grid">
          <div class="metric-item"><span class="kpi-label">来源页面</span><strong>{{ selectedResult.source_reference }} · {{ selectedResult.source_capture_label }}</strong></div>
          <div class="metric-item"><span class="kpi-label">历史证据</span><strong>{{ evidenceLabel(selectedResult.evidence_level) }}</strong></div>
          <div class="metric-item"><span class="kpi-label">回放范围</span><strong>{{ replayScopeLabel(selectedResult.replay_scope) }}</strong></div>
          <div class="metric-item"><span class="kpi-label">终点</span><strong>{{ terminalOutcomeLabel(selectedResult.terminal_outcome) }}</strong></div>
          <div class="metric-item"><span class="kpi-label">耗时</span><strong>{{ formatDuration(selectedResult.duration_ms) }}</strong></div>
        </div>
        <div v-if="hasSafetyBoundary(selectedResult)" class="boundary-evidence-grid">
          <div>
            <span>源巡检报告</span>
            <strong>{{ sourceBoundaryEvidenceLabel(selectedResult.source_boundary_evidence) }}</strong>
          </div>
          <div>
            <span>升级后执行</span>
            <strong>{{ replayBoundaryEvidenceLabel(selectedResult.replay_boundary_evidence) }}</strong>
          </div>
        </div>
        <div class="replay-evidence-grid">
          <div>
            <div class="image-title">当前页面截图</div>
            <img v-if="resultAssetUrl(selectedResult, 'candidate_screenshot')" :src="resultAssetUrl(selectedResult, 'candidate_screenshot')" alt="replay screenshot" />
            <div v-else-if="hasResultAsset(selectedResult, 'candidate_screenshot')" class="image-placeholder muted-text">{{ resultAssetNotice(selectedResult, 'candidate_screenshot') || '加载中' }}</div>
            <div v-else class="image-placeholder muted-text">暂无截图</div>
          </div>
          <div class="replay-xml-box">
            <div class="image-title">当前 XML</div>
            <el-button
              v-if="selectedResult.candidate_xml_asset_id || selectedResult.candidate_xml_path"
              link
              type="primary"
              @click="openXmlAsset(selectedResult.candidate_xml_asset_id, selectedResult.candidate_xml_path)"
            >查看 XML</el-button>
            <span v-else class="muted-text">暂无 XML</span>
          </div>
        </div>
        <div class="trace-section">
          <div class="detail-section-title">步骤 Trace</div>
          <el-empty v-if="!selectedReplayTrace.length" description="暂无步骤 Trace" :image-size="56" />
          <el-timeline v-else>
            <el-timeline-item
              v-for="step in selectedReplayTrace"
              :key="`${selectedResult.id}-${step.index}`"
              :type="replayStepType(step.status)"
              :timestamp="formatDuration(step.duration_ms)"
            >
              <div class="trace-title">{{ step.index + 1 }}. {{ step.name }} <el-tag size="small" effect="plain">{{ step.status_label }}</el-tag></div>
              <div v-if="step.expected_role || step.actual_role" class="trace-line">页面：{{ step.expected_role_label }} → {{ step.actual_role_label }}</div>
              <div v-if="step.reason" class="trace-line muted-text">{{ step.reason }}</div>
            </el-timeline-item>
          </el-timeline>
        </div>
        <el-collapse class="technical-collapse">
          <el-collapse-item title="技术信息" name="technical">
            <div class="technical-grid">
              <div><span>链路 ID</span><code>{{ selectedResult.chain_id || '-' }}</code></div>
              <div><span>Path key</span><code>{{ selectedResult.path_key || '-' }}</code></div>
              <div><span>State ID</span><code>{{ selectedResult.source_state_id || '-' }}</code></div>
              <div><span>Observation ID</span><code>{{ selectedResult.source_observation_id || '-' }}</code></div>
              <div><span>原始状态</span><code>{{ statusText(selectedResult.status) }}</code></div>
              <div><span>失败类型</span><code>{{ selectedResult.failure_type || '-' }}</code></div>
              <div><span>源边界证据</span><code>{{ selectedResult.source_boundary_evidence || '-' }}</code></div>
              <div><span>升级执行边界</span><code>{{ selectedResult.replay_boundary_evidence || '-' }}</code></div>
            </div>
            <div v-if="selectedResult.reason" class="technical-reason"><span>执行说明</span><code>{{ selectedResult.reason }}</code></div>
          </el-collapse-item>
        </el-collapse>
      </div>
      <div v-else-if="selectedResult" class="result-detail">
        <div class="result-header">
          <div>
            <h3>{{ selectedResult.page_name }}</h3>
            <p class="muted-text">{{ formatDevice(selectedResult.device_serial) }}</p>
          </div>
          <el-tag :type="statusType(selectedResult.status)" effect="plain">{{ statusLabel(selectedResult.status) }}</el-tag>
        </div>
        <el-alert v-if="selectedResult.reason" :title="selectedResult.reason" type="warning" show-icon :closable="false" />
        <div class="metric-grid">
          <div v-for="item in displayMetrics(selectedResult.metrics)" :key="item.key" class="metric-item">
            <span class="kpi-label">{{ formatMetricLabel(item.key) }}</span>
            <strong>{{ formatMetricValue(item.value) }}</strong>
          </div>
        </div>
        <div class="image-grid">
          <div>
            <div class="image-title">{{ isDeviceCompare ? '基准设备' : '旧版' }}</div>
            <img v-if="resultAssetUrl(selectedResult, 'baseline_screenshot')" :src="resultAssetUrl(selectedResult, 'baseline_screenshot')" alt="baseline" />
            <div v-else-if="hasResultAsset(selectedResult, 'baseline_screenshot')" class="image-placeholder muted-text">{{ resultAssetNotice(selectedResult, 'baseline_screenshot') || '加载中' }}</div>
          </div>
          <div>
            <div class="image-title">{{ isDeviceCompare ? '当前设备' : '新版' }}</div>
            <img v-if="resultAssetUrl(selectedResult, 'candidate_screenshot')" :src="resultAssetUrl(selectedResult, 'candidate_screenshot')" alt="candidate" />
            <div v-else-if="hasResultAsset(selectedResult, 'candidate_screenshot')" class="image-placeholder muted-text">{{ resultAssetNotice(selectedResult, 'candidate_screenshot') || '加载中' }}</div>
          </div>
          <div>
            <div class="image-title">差异</div>
            <img v-if="resultAssetUrl(selectedResult, 'diff_screenshot')" :src="resultAssetUrl(selectedResult, 'diff_screenshot')" alt="diff" />
            <div v-else-if="hasResultAsset(selectedResult, 'diff_screenshot')" class="image-placeholder muted-text">{{ resultAssetNotice(selectedResult, 'diff_screenshot') || '加载中' }}</div>
            <div v-else class="image-placeholder muted-text">{{ diffPlaceholder(selectedResult) }}</div>
          </div>
        </div>
        <div class="xml-summary">
          <div>
            <span class="kpi-label">{{ isDeviceCompare ? '基准 XML' : '旧版 XML' }}</span>
            <el-button
              v-if="selectedResult.baseline_xml_asset_id || selectedResult.baseline_xml_path"
              link
              type="primary"
              @click="openXmlAsset(selectedResult.baseline_xml_asset_id, selectedResult.baseline_xml_path)"
            >查看 XML</el-button>
            <strong v-else>-</strong>
          </div>
          <div>
            <span class="kpi-label">{{ isDeviceCompare ? '当前 XML' : '新版 XML' }}</span>
            <el-button
              v-if="selectedResult.candidate_xml_asset_id || selectedResult.candidate_xml_path"
              link
              type="primary"
              @click="openXmlAsset(selectedResult.candidate_xml_asset_id, selectedResult.candidate_xml_path)"
            >查看 XML</el-button>
            <strong v-else>-</strong>
          </div>
          <div>
            <span class="kpi-label">必需文本</span>
            <strong>{{ selectedResult.required_text || '-' }}</strong>
          </div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.compat-report-detail {
  flex: 1;
  height: 0;
  overflow: hidden;
  background: #f2f3f5;
}

.content-wrapper {
  height: calc(100% - 20px);
  margin: 10px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: #fff;
  border-radius: 4px;
  overflow: hidden;
}

.detail-header,
.header-actions,
.result-header {
  display: flex;
  align-items: center;
  gap: 12px;
}

.detail-header,
.result-header {
  justify-content: space-between;
}

.title-block h2 {
  margin: 4px 0;
  font-size: 20px;
  color: #303133;
}

.title-block p {
  margin: 0;
  color: #909399;
  font-size: 13px;
}

.kpi-row,
.metric-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.kpi-item,
.metric-item {
  min-height: 42px;
  padding: 7px 10px;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  background: #fafafa;
  display: flex;
  align-items: center;
  gap: 8px;
}

.kpi-label,
.muted-text,
.stage-text {
  color: #909399;
  font-size: 12px;
}

.stage-text {
  margin-left: 8px;
}

.run-name {
  font-weight: 600;
  color: #303133;
}

.pass {
  color: #67C23A;
}

.warn {
  color: #E6A23C;
}

.fail {
  color: #F56C6C;
}

.matrix-panel {
  flex: 1;
  min-height: 0;
}

.replay-panel {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.replay-stage {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.replay-stage > div {
  display: flex;
  align-items: center;
  gap: 10px;
}

.version-kpi {
  max-width: 520px;
}

.replay-path-label,
.result-summary {
  margin-top: 3px;
  line-height: 1.45;
}

.boundary-inline {
  margin-top: 4px;
  display: flex;
  flex-direction: column;
  line-height: 1.4;
}

.matrix-table {
  height: 100%;
}

.gallery-panel {
  flex-shrink: 0;
  border-top: 1px solid #ebeef5;
  padding-top: 10px;
}

.gallery-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.gallery-header .card-title {
  font-size: 14px;
  font-weight: 700;
  color: #303133;
}

.gallery-page-select {
  width: 220px;
}

.gallery-row {
  display: flex;
  gap: 10px;
  overflow-x: auto;
  padding-bottom: 6px;
}

.gallery-card {
  flex: 0 0 156px;
  padding: 6px;
  border: 2px solid #dcdfe6;
  border-radius: 6px;
  background: #fafafa;
  cursor: pointer;
}

.gallery-caption {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  font-size: 12px;
  font-weight: 600;
  color: #303133;
}

.gallery-device {
  display: flex;
  align-items: center;
  gap: 4px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.gallery-meta {
  margin: 2px 0 4px;
}

.gallery-card img {
  width: 100%;
  height: 216px;
  object-fit: contain;
  border-radius: 4px;
  background: #111;
}

.gallery-empty {
  height: 216px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.empty-state {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #909399;
}

.result-detail {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.result-header h3 {
  margin: 0;
}

.image-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.image-title {
  margin-bottom: 6px;
  font-size: 13px;
  font-weight: 700;
  color: #606266;
}

.image-grid img {
  width: 100%;
  max-height: 520px;
  object-fit: contain;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  background: #111;
}

.image-placeholder {
  min-height: 180px;
  border: 1px solid #dcdfe6;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #fafafa;
}

.replay-evidence-grid {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) minmax(180px, 0.5fr);
  gap: 12px;
}

.boundary-evidence-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1px;
  border: 1px solid #d9ecff;
  background: #d9ecff;
}

.boundary-evidence-grid > div {
  min-height: 60px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 5px;
  background: #f4f9ff;
}

.boundary-evidence-grid span,
.technical-grid span,
.technical-reason span {
  color: #909399;
  font-size: 12px;
}

.boundary-evidence-grid strong {
  color: #303133;
  font-size: 13px;
}

.replay-evidence-grid img {
  width: 100%;
  max-height: 520px;
  object-fit: contain;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  background: #111;
}

.replay-xml-box {
  min-height: 120px;
  padding: 12px;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  background: #fafafa;
}

.trace-section {
  padding-top: 12px;
  border-top: 1px solid #ebeef5;
}

.technical-collapse {
  border-top: 1px solid #ebeef5;
}

.technical-collapse :deep(.el-collapse-item__header) {
  color: #606266;
  font-size: 13px;
}

.technical-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.technical-grid > div,
.technical-reason {
  min-width: 0;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  border: 1px solid #ebeef5;
  background: #fafafa;
}

.technical-grid code,
.technical-reason code {
  overflow-wrap: anywhere;
  color: #606266;
  font-size: 11px;
}

.technical-reason {
  margin-top: 8px;
}

.detail-section-title,
.trace-title {
  color: #303133;
  font-weight: 600;
}

.detail-section-title {
  margin-bottom: 12px;
}

.trace-line {
  margin-top: 5px;
  color: #606266;
  font-size: 12px;
  word-break: break-all;
}

.xml-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.xml-summary > div {
  min-height: 48px;
  padding: 8px 10px;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  background: #fafafa;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.xml-summary strong {
  font-size: 12px;
  color: #606266;
  word-break: break-all;
}

@media (max-width: 800px) {
  .compat-report-detail { overflow: auto; }
  .content-wrapper { height: auto; min-height: calc(100% - 20px); overflow: visible; }
  .replay-panel { min-height: 520px; }
  .replay-stage { align-items: flex-start; flex-direction: column; }
  .replay-evidence-grid, .image-grid, .xml-summary, .boundary-evidence-grid, .technical-grid { grid-template-columns: 1fr; }
}
</style>
