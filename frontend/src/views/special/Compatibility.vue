<script setup>
import { computed, onActivated, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Collection, Refresh, View } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import api from '@/api'
import { runStatusTagType } from '@/utils/statusMeta'
import {
  compatibilityStatusLabel,
  packageSnapshotLabel,
} from '@/utils/compatibilityReplay'

const CURRENT_BASELINE_VALUE = '__current_installed__'

const router = useRouter()
const packages = ref([])
const devices = ref([])
const environments = ref([])
const pageSets = ref([])
const recentRuns = ref([])
const loading = reactive({
  bootstrap: false,
  reports: false,
  submitting: false,
})

const form = reactive({
  name: '',
  compare_mode: 'version',
  old_package_id: CURRENT_BASELINE_VALUE,
  new_package_id: '',
  page_set_id: '',
  device_serials: [],
  baseline_device_serial: '',
  mode: 'upgrade',
  env_id: null,
  pixel_diff_ratio_warn: 0.03,
  ssim_warn: 0.96,
  xml_diff_ratio_warn: 0.35,
})

const androidDevices = computed(() => (
  (devices.value || []).filter(item => (
    String(item.platform || 'android').toLowerCase() === 'android'
    && String(item.status || '').toUpperCase() !== 'OFFLINE'
  ))
))

const isDeviceCompare = computed(() => form.compare_mode === 'device')
const isCurrentBaseline = computed(() => form.old_package_id === CURRENT_BASELINE_VALUE)
const oldPackage = computed(() => (
  isCurrentBaseline.value ? null : packages.value.find(item => item.id === form.old_package_id)
))
const newPackageOptions = computed(() => {
  if (isDeviceCompare.value || isCurrentBaseline.value || !oldPackage.value?.package_name) return packages.value
  return packages.value.filter(item => (
    item.package_name === oldPackage.value.package_name && item.id !== form.old_package_id
  ))
})

const formatDevice = serial => {
  const device = devices.value.find(item => item.serial === serial)
  return device?.custom_name || device?.market_name || device?.model || serial || '-'
}
const formatPackage = pkg => packageSnapshotLabel(pkg)
const formatTime = value => value ? dayjs(value).format('MM-DD HH:mm:ss') : '-'
const statusText = status => String(status || 'PENDING').toUpperCase()
const statusType = status => runStatusTagType(statusText(status))
const recentRunModeLabel = row => (
  { version: '版本对比', device: '机型对比' }[row.compare_mode] || '兼容性对比'
)
const recentRunItemCount = row => row.page_set?.pages?.length || row.total_pages || 0

const errorMessage = (error, fallback) => {
  const detail = error.response?.data?.detail
  return typeof detail === 'string' ? detail : detail?.message || error.message || fallback
}

const fetchPackages = async () => {
  const pageSize = 100
  const all = []
  for (let page = 1; page <= 20; page += 1) {
    const { data } = await api.getPackages({ page, page_size: pageSize, platform: 'android' })
    const items = data.items || []
    all.push(...items)
    if (all.length >= Number(data.total ?? all.length) || items.length < pageSize) break
  }
  packages.value = all
}
const fetchDevices = async () => {
  const { data } = await api.getDeviceList()
  devices.value = data || []
}
const fetchEnvironments = async () => {
  const { data } = await api.getEnvironments()
  environments.value = data || []
}
const fetchPageSets = async () => {
  const { data } = await api.getCompatPageSets()
  pageSets.value = data || []
  if (!form.page_set_id && pageSets.value.length) form.page_set_id = pageSets.value[0].id
}
const fetchRecentRuns = async () => {
  loading.reports = true
  try {
    const { data } = await api.getCompatibilityRuns({ skip: 0, limit: 5 })
    recentRuns.value = data.items || []
  } finally {
    loading.reports = false
  }
}

const refreshAll = async () => {
  loading.bootstrap = true
  try {
    await Promise.all([
      fetchDevices(),
      fetchRecentRuns(),
      fetchPackages(),
      fetchEnvironments(),
      fetchPageSets(),
    ])
  } catch (error) {
    ElMessage.error(errorMessage(error, '加载兼容性测试数据失败'))
  } finally {
    loading.bootstrap = false
  }
}

const buildRunName = () => {
  if (form.name) return
  const newPkg = packages.value.find(item => item.id === form.new_package_id)
  if (!newPkg) return
  if (isDeviceCompare.value) form.name = `机型对比 ${formatPackage(newPkg)}`
  else form.name = `${formatPackage(oldPackage.value) || '当前版本'} -> ${formatPackage(newPkg)}`
}
watch(() => form.new_package_id, buildRunName)
watch(() => form.compare_mode, mode => {
  if (mode === 'device') {
    form.mode = 'clean'
    form.baseline_device_serial ||= form.device_serials[0] || ''
  } else if (mode === 'version') form.mode = 'upgrade'
})

const submitRun = async () => {
  if (loading.submitting) return
  if (!form.new_package_id) return ElMessage.warning('请选择待测 APK')
  if (!form.page_set_id) return ElMessage.warning('请选择页面合集')
  if (!form.device_serials.length) return ElMessage.warning('请选择测试设备')
  if (isDeviceCompare.value && form.device_serials.length < 2) return ElMessage.warning('机型对比至少选择两台设备')
  loading.submitting = true
  try {
    const { data } = await api.createCompatibilityRun({
      name: form.name.trim() || `兼容性对比 ${dayjs().format('MM-DD HH:mm')}`,
      source_type: 'page_set',
      compare_mode: form.compare_mode,
      old_package_id: (isDeviceCompare.value || isCurrentBaseline.value) ? null : form.old_package_id,
      new_package_id: form.new_package_id,
      page_set_id: form.page_set_id,
      device_serials: form.device_serials,
      baseline_device_serial: isDeviceCompare.value ? form.baseline_device_serial : null,
      mode: form.mode,
      env_id: form.env_id || null,
      thresholds: {
        pixel_diff_ratio_warn: Number(form.pixel_diff_ratio_warn),
        ssim_warn: Number(form.ssim_warn),
        xml_diff_ratio_warn: Number(form.xml_diff_ratio_warn),
      },
    })
    ElMessage.success('兼容性对比任务已提交')
    router.push(`/execution/reports/compatibility/${data.id}`)
  } catch (error) {
    ElMessage.error(errorMessage(error, '提交失败'))
  } finally {
    loading.submitting = false
  }
}

const goPageSets = () => router.push('/special/compatibility/page-sets')
const goReports = () => router.push({ path: '/execution/reports', query: { tab: 'compatibility' } })
const goReportDetail = id => router.push(`/execution/reports/compatibility/${id}`)

onMounted(refreshAll)
onActivated(refreshAll)
</script>

<template>
  <div class="compat-run-page" v-loading="loading.bootstrap">
    <div class="content-wrapper">
      <el-card shadow="never" class="config-card">
        <template #header>
          <div class="card-header">
            <div>
              <div class="card-title">新建兼容性测试</div>
              <div class="muted-text">用页面合集在指定页面上对比旧版 / 新版，或对比两台设备</div>
            </div>
            <div class="header-actions">
              <el-tooltip content="刷新设备和报告" placement="top">
                <el-button :icon="Refresh" circle aria-label="刷新设备和报告" @click="refreshAll" />
              </el-tooltip>
            </div>
          </div>
        </template>

        <el-form label-position="top" class="compat-form">
          <div class="form-grid">
            <el-form-item label="任务名称"><el-input v-model="form.name" clearable /></el-form-item>
            <el-form-item label="对比维度"><el-segmented v-model="form.compare_mode" :options="[{ label: '版本对比', value: 'version' }, { label: '机型对比', value: 'device' }]" /></el-form-item>
            <el-form-item label="执行模式"><el-segmented v-model="form.mode" :options="[{ label: '升级兼容', value: 'upgrade' }, { label: '干净对比', value: 'clean' }]" /></el-form-item>
            <el-form-item label="变量环境">
              <el-select v-model="form.env_id" clearable filterable placeholder="不使用环境变量"><el-option v-for="env in environments" :key="env.id" :label="env.name" :value="env.id" /></el-select>
            </el-form-item>
          </div>
          <div class="form-grid">
            <el-form-item v-if="!isDeviceCompare" label="旧版 APK">
              <el-select v-model="form.old_package_id" filterable class="wide-control">
                <el-option label="当前版本" :value="CURRENT_BASELINE_VALUE" />
                <el-option v-for="pkg in packages" :key="pkg.id" :label="formatPackage(pkg)" :value="pkg.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="待测 APK">
              <el-select v-model="form.new_package_id" filterable class="wide-control"><el-option v-for="pkg in newPackageOptions" :key="pkg.id" :label="formatPackage(pkg)" :value="pkg.id" /></el-select>
            </el-form-item>
            <el-form-item label="测试设备">
              <el-select v-model="form.device_serials" multiple filterable collapse-tags class="wide-control">
                <el-option v-for="device in androidDevices" :key="device.serial" :label="formatDevice(device.serial)" :value="device.serial" :disabled="String(device.status).toUpperCase() !== 'IDLE'" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="isDeviceCompare" label="基准设备">
              <el-select v-model="form.baseline_device_serial" class="wide-control"><el-option v-for="serial in form.device_serials" :key="serial" :label="formatDevice(serial)" :value="serial" /></el-select>
            </el-form-item>
          </div>
          <el-form-item label="页面合集">
            <div class="page-set-row"><el-select v-model="form.page_set_id" class="page-set-select"><el-option v-for="set in pageSets" :key="set.id" :label="`${set.name} (${set.pages?.length || 0})`" :value="set.id" /></el-select><el-button :icon="Collection" @click="goPageSets">管理合集</el-button></div>
          </el-form-item>
          <div class="threshold-grid">
            <el-form-item label="像素差异"><el-input-number v-model="form.pixel_diff_ratio_warn" :min="0" :max="1" :step="0.01" /></el-form-item>
            <el-form-item label="SSIM"><el-input-number v-model="form.ssim_warn" :min="0" :max="1" :step="0.01" /></el-form-item>
            <el-form-item label="XML 差异"><el-input-number v-model="form.xml_diff_ratio_warn" :min="0" :max="1" :step="0.05" /></el-form-item>
          </div>
          <div class="form-actions"><el-button type="primary" :loading="loading.submitting" @click="submitRun">开始对比</el-button></div>
        </el-form>
      </el-card>

      <el-card shadow="never" class="reports-card">
        <template #header>
          <div class="card-header">
            <span class="card-title">最新兼容性报告</span>
            <div class="header-actions"><el-button link type="primary" @click="goReports">前往报告中心</el-button><el-button :icon="Refresh" @click="fetchRecentRuns">刷新</el-button></div>
          </div>
        </template>
        <el-table :data="recentRuns" v-loading="loading.reports" height="100%" row-key="id">
          <el-table-column label="任务" min-width="220"><template #default="{ row }"><div class="run-name">{{ row.name }}</div><div class="muted-text">{{ row.package_name }}</div></template></el-table-column>
          <el-table-column label="页面合集 / 模式" min-width="190"><template #default="{ row }"><div>{{ row.page_set?.name || '-' }}</div><div class="muted-text">{{ recentRunModeLabel(row) }}</div></template></el-table-column>
          <el-table-column label="设备 / 页面" width="100" align="center"><template #default="{ row }">{{ row.total_cells }} / {{ recentRunItemCount(row) }}</template></el-table-column>
          <el-table-column label="结果" width="190" align="center">
            <template #default="{ row }">
              <div class="result-summary" aria-label="执行结果数量">
                <span class="summary-count pass">通过 {{ row.pass_count || 0 }}</span>
                <span class="summary-count warn">关注 {{ row.warning_count || 0 }}</span>
                <span class="summary-count fail">失败 {{ row.fail_count || 0 }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="110" align="center"><template #default="{ row }"><el-tag :type="statusType(row.status)" size="small" effect="plain">{{ compatibilityStatusLabel(row.status) }}</el-tag></template></el-table-column>
          <el-table-column label="开始时间" width="150" align="center"><template #default="{ row }">{{ formatTime(row.started_at || row.created_at) }}</template></el-table-column>
          <el-table-column label="操作" width="80" align="center" fixed="right"><template #default="{ row }"><el-tooltip content="查看报告" placement="top"><el-button :icon="View" link type="primary" @click="goReportDetail(row.id)" /></el-tooltip></template></el-table-column>
        </el-table>
      </el-card>
    </div>
  </div>
</template>

<style scoped>
.compat-run-page { flex: 1; height: 0; overflow: auto; background: var(--ad-bg); }
.content-wrapper { min-height: calc(100% - 32px); margin: 16px; display: flex; flex-direction: column; gap: 12px; }
.config-card, .reports-card { border-radius: var(--ad-radius); }
.reports-card { min-height: 300px; }
.card-header, .header-actions, .page-set-row { display: flex; align-items: center; gap: 12px; }
.card-header { justify-content: space-between; }
.card-title { font-size: 15px; font-weight: 700; color: var(--ad-text); }
.compat-form :deep(.el-form-item) { margin-bottom: 16px; }
.compat-form :deep(.el-form-item__label) { padding-bottom: 4px; line-height: 18px; font-weight: 600; color: var(--ad-muted); }
.form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 0 16px; }
.threshold-grid { display: grid; grid-template-columns: repeat(3, minmax(180px, 1fr)); gap: 0 16px; padding-top: 8px; border-top: 1px solid var(--ad-border); }
.wide-control, .page-set-select { width: 100%; }
.page-set-select { max-width: 560px; }
.form-actions { display: flex; justify-content: flex-end; }
.run-name { font-weight: 600; color: var(--ad-text); }
.muted-text { color: var(--ad-muted); font-size: 12px; }
.result-summary { display: inline-flex; align-items: center; justify-content: center; gap: 8px; white-space: nowrap; }
.summary-count { font-size: 12px; font-weight: 600; }
.pass { color: var(--ad-success); } .warn { color: var(--ad-warning); } .fail { color: var(--ad-danger); }
@media (max-width: 960px) {
  .card-header { align-items: flex-start; flex-direction: column; }
  .header-actions { width: 100%; flex-wrap: wrap; }
}
@media (max-width: 600px) {
  .form-grid, .threshold-grid { grid-template-columns: 1fr; }
}

@media (max-width: 767px) {
  :deep(.el-button), :deep(.el-radio-button__inner), :deep(.el-select__wrapper) { min-height: 44px; }
  :deep(.el-input__inner), :deep(.el-textarea__inner) { font-size: 16px; }
  :deep(.el-form-item__label), :deep(.el-table), :deep(.el-descriptions), :deep(.el-tabs__item) { font-size: 14px; }
  :deep(.el-table__cell) { font-size: 14px; }
}
</style>
