<script setup>
import { chartColors } from '@/utils/chartTheme'
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import dayjs from 'dayjs'
import api from '@/api'
import { runStatusTagType } from '@/utils/statusMeta'
import { createPollingLoop, pollRequestConfig } from '@/utils/polling'
import { compatibilityStatusLabel } from '@/utils/compatibilityReplay'

const route = useRoute()
const router = useRouter()
const run = ref(null)
const devices = ref([])
const selectedResult = ref(null)
const resultDrawerVisible = ref(false)
const loadError = ref('')
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
let pageActive = false
let assetLifecycleVersion = 0

const matrixPages = computed(() => run.value?.page_set?.pages || [])
const isDeviceCompare = computed(() => run.value?.compare_mode === 'device')
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
  PASS: chartColors.success,
  WARNING: chartColors.warning,
  FAIL: chartColors.danger,
  ERROR: chartColors.danger,
}[statusText(status)] || chartColors.border)
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
])
const formatMode = (mode) => mode === 'clean' ? '干净对比' : '升级兼容'
const formatCompareMode = (mode) => ({
  version: '版本对比',
  device: '机型对比',
}[mode] || '兼容性对比')
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
// config：真轮询传 pollRequestConfig()（8s 短超时，慢响应不挂满轮询周期）；
// 一次性首载/取消后刷新/手动重载不传，走 api 层默认 30s（大载荷详情）。
const fetchRun = async (config) => {
  const id = Number(route.params.id)
  if (!id) return
  loading.value = true
  loadError.value = ''
  try {
    const { data } = await api.getCompatibilityRun(id, config)
    run.value = data
  } catch (err) {
    loadError.value = '加载兼容性报告失败，请重试'
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

// 运行详情轮询：在途不重叠 + 页面隐藏即暂停（utils/polling.js 统一门闩）
const RUN_POLL_INTERVAL_MS = 8000
const runPollLoop = createPollingLoop({
  intervalMs: RUN_POLL_INTERVAL_MS,
  tick: async () => {
    // 真轮询：8s 短超时（旧实现不传 config，每轮挂满 30s 默认超时）
    if (hasRunningRun.value) await fetchRun(pollRequestConfig())
  }
})
const startPolling = () => {
  runPollLoop.start()
}
const stopPolling = () => {
  runPollLoop.stop()
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

        <el-alert v-if="loadError" :title="loadError" type="error" :closable="false"><el-button link @click="fetchRun()">重新加载</el-button></el-alert>

      <div v-if="run" class="kpi-row">
        <div class="kpi-item">
          <span class="kpi-label">状态</span>
          <el-tag :type="statusType(run.status)" effect="plain">{{ statusLabel(run.status) }}</el-tag>
        </div>
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

      <section v-if="run" class="matrix-panel">
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

      <section v-if="run && galleryCards.length" class="gallery-panel">
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

    <el-drawer v-model="resultDrawerVisible" size="min(960px, 100vw)" title="页面对比详情">
      <div v-if="selectedResult" class="result-detail">
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
  background: var(--ad-bg);
}

.content-wrapper {
  height: calc(100% - 32px);
  margin: 16px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: var(--ad-surface);
  border-radius: var(--ad-radius);
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
  color: var(--ad-text);
}

.title-block p {
  margin: 0;
  color: var(--ad-muted);
  font-size: 13px;
}

.kpi-row,
.metric-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.kpi-item,
.metric-item {
  min-height: 42px;
  padding: 7px 10px;
  border: 1px solid var(--ad-border);
  border-radius: var(--ad-radius);
  background: var(--ad-bg);
  display: flex;
  align-items: center;
  gap: 8px;
}

.kpi-label,
.muted-text,
.stage-text {
  color: var(--ad-muted);
  font-size: 12px;
}

.stage-text {
  margin-left: 8px;
}

.run-name {
  font-weight: 600;
  color: var(--ad-text);
}

.pass {
  color: var(--ad-success);
}

.warn {
  color: var(--ad-warning);
}

.fail {
  color: var(--ad-danger);
}

.matrix-panel {
  flex: 1;
  min-height: 0;
}

.matrix-table {
  height: 100%;
}

.gallery-panel {
  flex-shrink: 0;
  border-top: 1px solid var(--ad-border);
  padding-top: 10px;
}

.gallery-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.gallery-header .card-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--ad-text);
}

.gallery-page-select {
  width: 220px;
}

.gallery-row {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding-bottom: 6px;
}

.gallery-card {
  flex: 0 0 156px;
  padding: 6px;
  border: 2px solid var(--ad-border);
  border-radius: 6px;
  background: var(--ad-bg);
  cursor: pointer;
}

.gallery-caption {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  font-size: 12px;
  font-weight: 600;
  color: var(--ad-text);
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
  border-radius: var(--ad-radius);
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
  color: var(--ad-muted);
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
  color: var(--ad-muted);
}

.image-grid img {
  width: 100%;
  max-height: 520px;
  object-fit: contain;
  border: 1px solid var(--ad-border);
  border-radius: var(--ad-radius);
  background: #111;
}

.image-placeholder {
  min-height: 180px;
  border: 1px solid var(--ad-border);
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--ad-bg);
}

.xml-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.xml-summary > div {
  min-height: 48px;
  padding: 8px 10px;
  border: 1px solid var(--ad-border);
  border-radius: var(--ad-radius);
  background: var(--ad-bg);
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.xml-summary strong {
  font-size: 12px;
  color: var(--ad-muted);
  word-break: break-all;
}

@media (max-width: 800px) {
  .compat-report-detail { overflow: auto; }
  .content-wrapper { height: auto; min-height: calc(100% - 32px); overflow: visible; }
  .image-grid, .xml-summary { grid-template-columns: 1fr; }
}

@media (max-width: 767px) {
  :deep(.el-button), :deep(.el-radio-button__inner), :deep(.el-select__wrapper) { min-height: 44px; }
  :deep(.el-input__inner), :deep(.el-textarea__inner) { font-size: 16px; }
  :deep(.el-form-item__label), :deep(.el-table), :deep(.el-descriptions), :deep(.el-tabs__item) { font-size: 14px; }
  :deep(.el-table__cell) { font-size: 14px; }
}
@media (max-width: 767px) {
  .content-wrapper { margin: 8px; padding: 12px; height: calc(100% - 16px); }
  .detail-header, .header-actions, .result-header { flex-wrap: wrap; }
  .title-block { min-width: 0; }
  .title-block h2, .title-block p { overflow-wrap: anywhere; }
  .kpi-item, .metric-item { min-width: 0; max-width: 100%; font-size: 14px; }
}
</style>
