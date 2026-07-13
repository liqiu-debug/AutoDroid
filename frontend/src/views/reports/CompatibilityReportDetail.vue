<script setup>
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import dayjs from 'dayjs'
import api from '@/api'
import { runStatusTagType } from '@/utils/statusMeta'

const route = useRoute()
const router = useRouter()
const run = ref(null)
const devices = ref([])
const selectedResult = ref(null)
const resultDrawerVisible = ref(false)
const loading = ref(false)
let pollTimer = null
let pageActive = false

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
const hiddenMetricKeys = new Set(['ocr_diff_ratio', 'ocr_error_baseline', 'ocr_error_candidate'])
const formatMode = (mode) => mode === 'clean' ? '干净对比' : '升级兼容'
const formatCompareMode = (mode) => mode === 'device' ? '机型对比' : '版本对比'
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
const assetUrl = (path) => path ? api.getReportAssetUrl(path) : ''
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
  refreshAll()
})

onMounted(async () => {
  await refreshAll()
  activatePage()
})
onActivated(() => {
  refreshAll()
  activatePage()
})
onDeactivated(deactivatePage)
onUnmounted(deactivatePage)
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

      <div v-if="run" class="kpi-row">
        <div class="kpi-item">
          <span class="kpi-label">状态</span>
          <el-tag :type="statusType(run.status)" effect="plain">{{ statusText(run.status) }}</el-tag>
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
              <el-tag :type="statusType(row.status)" size="small" effect="plain">{{ statusText(row.status) }}</el-tag>
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
                {{ statusText(findPageResult(row, page).status) }}
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
                {{ statusText(card.result.status) }}
              </el-tag>
            </div>
            <div class="gallery-meta muted-text">{{ card.cell.resolution || '-' }}</div>
            <img
              v-if="card.result?.candidate_screenshot_path"
              :src="assetUrl(card.result.candidate_screenshot_path)"
              :alt="card.cell.device_serial"
              loading="lazy"
            />
            <div v-else class="gallery-empty muted-text">暂无截图</div>
          </div>
        </div>
      </section>

      <div v-else-if="!run" class="empty-state">暂无兼容性报告详情</div>
    </div>

    <el-drawer v-model="resultDrawerVisible" size="64%" title="页面对比详情">
      <div v-if="selectedResult" class="result-detail">
        <div class="result-header">
          <div>
            <h3>{{ selectedResult.page_name }}</h3>
            <p class="muted-text">{{ formatDevice(selectedResult.device_serial) }}</p>
          </div>
          <el-tag :type="statusType(selectedResult.status)" effect="plain">{{ statusText(selectedResult.status) }}</el-tag>
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
            <img v-if="selectedResult.baseline_screenshot_path" :src="assetUrl(selectedResult.baseline_screenshot_path)" alt="baseline" />
          </div>
          <div>
            <div class="image-title">{{ isDeviceCompare ? '当前设备' : '新版' }}</div>
            <img v-if="selectedResult.candidate_screenshot_path" :src="assetUrl(selectedResult.candidate_screenshot_path)" alt="candidate" />
          </div>
          <div>
            <div class="image-title">差异</div>
            <img v-if="selectedResult.diff_screenshot_path" :src="assetUrl(selectedResult.diff_screenshot_path)" alt="diff" />
            <div v-else-if="isDeviceCompare" class="muted-text">跨分辨率不生成差异图</div>
          </div>
        </div>
        <div class="xml-summary">
          <div>
            <span class="kpi-label">{{ isDeviceCompare ? '基准 XML' : '旧版 XML' }}</span>
            <strong>{{ selectedResult.baseline_xml_path || '-' }}</strong>
          </div>
          <div>
            <span class="kpi-label">{{ isDeviceCompare ? '当前 XML' : '新版 XML' }}</span>
            <strong>{{ selectedResult.candidate_xml_path || '-' }}</strong>
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
</style>
