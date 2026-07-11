<script setup>
import { computed, onActivated, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Collection, Refresh, VideoPlay, View } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import api from '@/api'
import { runStatusTagType } from '@/utils/statusMeta'

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
  old_package_id: CURRENT_BASELINE_VALUE,
  new_package_id: '',
  page_set_id: '',
  device_serials: [],
  mode: 'upgrade',
  env_id: null,
  pixel_diff_ratio_warn: 0.03,
  ssim_warn: 0.96,
  xml_diff_ratio_warn: 0.35,
})

const isCurrentBaseline = computed(() => form.old_package_id === CURRENT_BASELINE_VALUE)
const oldPackage = computed(() => (
  isCurrentBaseline.value ? null : packages.value.find(item => item.id === form.old_package_id)
))
const newPackageOptions = computed(() => {
  if (isCurrentBaseline.value) return packages.value
  if (!oldPackage.value?.package_name) return packages.value
  return packages.value.filter(item => (
    item.package_name === oldPackage.value.package_name && item.id !== form.old_package_id
  ))
})
const androidDevices = computed(() => (
  (devices.value || []).filter(item => (
    String(item.platform || 'android').toLowerCase() === 'android'
    && String(item.status || '').toUpperCase() !== 'OFFLINE'
  ))
))

function buildDefaultRunName() {
  const newPkg = packages.value.find(item => item.id === form.new_package_id)
  if (!newPkg || form.name) return
  const targetVersion = newPkg.version_name || newPkg.version_code
  if (isCurrentBaseline.value) {
    form.name = `当前版本 -> ${newPkg.app_name || newPkg.package_name} ${targetVersion || ''}`.trim()
    return
  }
  const oldPkg = packages.value.find(item => item.id === form.old_package_id)
  if (!oldPkg) return
  form.name = `${oldPkg.app_name || oldPkg.package_name} ${oldPkg.version_name || oldPkg.version_code} -> ${targetVersion}`
}

watch(() => form.old_package_id, () => {
  if (form.new_package_id && !newPackageOptions.value.some(item => item.id === form.new_package_id)) {
    form.new_package_id = ''
  }
  buildDefaultRunName()
})

watch(() => form.new_package_id, buildDefaultRunName)

const formatPackage = (pkg) => {
  if (!pkg) return '-'
  const version = [pkg.version_name, pkg.version_code ? `(${pkg.version_code})` : ''].filter(Boolean).join(' ')
  return `${pkg.app_name || pkg.package_name} ${version}`.trim()
}
const formatDevice = (serial) => {
  const device = devices.value.find(item => item.serial === serial)
  return device?.custom_name || device?.market_name || device?.model || serial || '-'
}
const formatTime = (value) => value ? dayjs(value).format('MM-DD HH:mm:ss') : '-'
const statusText = (status) => String(status || 'PENDING').toUpperCase()
const statusType = (status) => runStatusTagType(statusText(status))

const fetchPackages = async () => {
  // 循环拉全量：包列表兼作旧/新版本配对与包名过滤的查找表，不适合改远程搜索
  const pageSize = 100
  const all = []
  for (let page = 1; page <= 20; page += 1) {
    const { data } = await api.getPackages({ page, page_size: pageSize })
    const items = data.items || []
    all.push(...items)
    const totalCount = Number(data.total ?? all.length)
    if (all.length >= totalCount || items.length < pageSize) break
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
  if (!form.page_set_id && pageSets.value.length) {
    form.page_set_id = pageSets.value[0].id
  }
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
    await Promise.all([fetchPackages(), fetchDevices(), fetchEnvironments(), fetchPageSets(), fetchRecentRuns()])
  } catch (err) {
    ElMessage.error(err.response?.data?.detail || err.message || '加载兼容性测试数据失败')
  } finally {
    loading.bootstrap = false
  }
}

const submitRun = async () => {
  if (!isCurrentBaseline.value && !form.old_package_id) return ElMessage.warning('请选择旧版 APK')
  if (!form.new_package_id) return ElMessage.warning('请选择新版 APK')
  if (!form.page_set_id) return ElMessage.warning('请选择页面合集')
  if (!form.device_serials.length) return ElMessage.warning('请选择测试设备')
  if (!form.name.trim()) return ElMessage.warning('请输入任务名称')

  loading.submitting = true
  try {
    const payload = {
      name: form.name.trim(),
      old_package_id: isCurrentBaseline.value ? null : form.old_package_id,
      new_package_id: form.new_package_id,
      page_set_id: form.page_set_id,
      device_serials: form.device_serials,
      mode: form.mode,
      env_id: form.env_id || null,
      thresholds: {
        pixel_diff_ratio_warn: Number(form.pixel_diff_ratio_warn),
        ssim_warn: Number(form.ssim_warn),
        xml_diff_ratio_warn: Number(form.xml_diff_ratio_warn),
      },
    }
    const { data } = await api.createCompatibilityRun(payload)
    ElMessage.success('兼容性任务已提交')
    router.push(`/execution/reports/compatibility/${data.id}`)
  } catch (err) {
    const detail = err.response?.data?.detail
    ElMessage.error(typeof detail === 'string' ? detail : detail?.message || err.message || '提交失败')
  } finally {
    loading.submitting = false
  }
}

const goPageSets = () => {
  router.push('/special/compatibility/page-sets')
}
const goReports = () => {
  router.push({ path: '/execution/reports', query: { tab: 'compatibility' } })
}
const goReportDetail = (id) => {
  router.push(`/execution/reports/compatibility/${id}`)
}

onMounted(refreshAll)
onActivated(refreshAll)
</script>

<template>
  <div class="compat-run-page" v-loading="loading.bootstrap">
    <div class="content-wrapper">
      <el-card shadow="never" class="config-card">
        <template #header>
          <div class="card-header">
            <span class="card-title">兼容性测试配置</span>
            <div class="header-actions">
              <el-button :icon="Refresh" @click="refreshAll">刷新</el-button>
              <el-button type="primary" :icon="VideoPlay" :loading="loading.submitting" @click="submitRun">开始测试</el-button>
            </div>
          </div>
        </template>

        <el-form label-position="top" class="compat-form">
          <div class="form-grid">
            <el-form-item label="任务名称">
              <el-input v-model="form.name" clearable />
            </el-form-item>
            <el-form-item label="执行模式">
              <el-segmented
                v-model="form.mode"
                :options="[
                  { label: '升级兼容', value: 'upgrade' },
                  { label: '干净对比', value: 'clean' },
                ]"
              />
            </el-form-item>
            <el-form-item label="变量环境">
              <el-select v-model="form.env_id" clearable filterable placeholder="不使用环境变量">
                <el-option v-for="env in environments" :key="env.id" :label="env.name" :value="env.id" />
              </el-select>
            </el-form-item>
          </div>

          <div class="form-grid">
            <el-form-item label="旧版 APK">
              <el-select v-model="form.old_package_id" filterable placeholder="选择旧版本" class="wide-control">
                <el-option label="当前版本" :value="CURRENT_BASELINE_VALUE">
                  <span>当前版本</span>
                  <span class="option-meta">com.ehaier.zgq.shop.mall</span>
                </el-option>
                <el-option
                  v-for="pkg in packages"
                  :key="pkg.id"
                  :label="formatPackage(pkg)"
                  :value="pkg.id"
                >
                  <span>{{ formatPackage(pkg) }}</span>
                  <span class="option-meta">{{ pkg.package_name }}</span>
                </el-option>
              </el-select>
            </el-form-item>
            <el-form-item label="新版 APK">
              <el-select v-model="form.new_package_id" filterable placeholder="选择新版本" class="wide-control">
                <el-option
                  v-for="pkg in newPackageOptions"
                  :key="pkg.id"
                  :label="formatPackage(pkg)"
                  :value="pkg.id"
                >
                  <span>{{ formatPackage(pkg) }}</span>
                  <span class="option-meta">{{ pkg.package_name }}</span>
                </el-option>
              </el-select>
            </el-form-item>
          </div>

          <el-form-item label="测试设备">
            <el-select
              v-model="form.device_serials"
              multiple
              filterable
              collapse-tags
              collapse-tags-tooltip
              placeholder="选择 Android 设备"
              class="wide-control"
            >
              <el-option
                v-for="device in androidDevices"
                :key="device.serial"
                :label="formatDevice(device.serial)"
                :value="device.serial"
                :disabled="device.status !== 'IDLE'"
              >
                <span>{{ formatDevice(device.serial) }}</span>
                <span class="option-meta">{{ device.serial }} / {{ device.status }}</span>
              </el-option>
            </el-select>
          </el-form-item>

          <el-form-item label="页面合集">
            <div class="page-set-row">
              <el-select v-model="form.page_set_id" filterable placeholder="选择页面合集" class="page-set-select">
                <el-option
                  v-for="set in pageSets"
                  :key="set.id"
                  :label="`${set.name} (${set.pages?.length || 0})`"
                  :value="set.id"
                />
              </el-select>
              <el-button :icon="Collection" @click="goPageSets">管理合集</el-button>
            </div>
          </el-form-item>

          <div class="threshold-grid">
            <el-form-item label="像素差异">
              <el-input-number v-model="form.pixel_diff_ratio_warn" :min="0" :max="1" :step="0.01" />
            </el-form-item>
            <el-form-item label="SSIM">
              <el-input-number v-model="form.ssim_warn" :min="0" :max="1" :step="0.01" />
            </el-form-item>
            <el-form-item label="XML差异">
              <el-input-number v-model="form.xml_diff_ratio_warn" :min="0" :max="1" :step="0.05" />
            </el-form-item>
          </div>
        </el-form>
      </el-card>

      <el-card shadow="never" class="reports-card">
        <template #header>
          <div class="card-header">
            <span class="card-title">最新兼容性报告</span>
            <div class="header-actions">
              <el-button link type="primary" @click="goReports">前往报告中心</el-button>
              <el-button :icon="Refresh" @click="fetchRecentRuns">刷新</el-button>
            </div>
          </div>
        </template>

        <el-table
          :data="recentRuns"
          v-loading="loading.reports"
          height="100%"
          row-key="id"
        >
          <el-table-column prop="id" label="ID" width="64" />
          <el-table-column label="任务" min-width="220">
            <template #default="{ row }">
              <div class="run-name">{{ row.name }}</div>
              <div class="muted-text">{{ row.package_name }}</div>
            </template>
          </el-table-column>
          <el-table-column label="页面合集" min-width="150">
            <template #default="{ row }">{{ row.page_set?.name || '-' }}</template>
          </el-table-column>
          <el-table-column label="设备/页面" width="100" align="center">
            <template #default="{ row }">{{ row.total_cells }} / {{ row.page_set?.pages?.length || row.total_pages || 0 }}</template>
          </el-table-column>
          <el-table-column label="结果" width="120" align="center">
            <template #default="{ row }">
              <span class="summary-count pass">{{ row.pass_count }}</span>
              <span class="summary-count warn">{{ row.warning_count }}</span>
              <span class="summary-count fail">{{ row.fail_count }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="110" align="center">
            <template #default="{ row }">
              <el-tag :type="statusType(row.status)" size="small" effect="plain">{{ statusText(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="开始时间" width="150" align="center">
            <template #default="{ row }">{{ formatTime(row.started_at || row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="80" align="center" fixed="right">
            <template #default="{ row }">
              <el-tooltip content="查看报告" placement="top">
                <el-button :icon="View" link type="primary" @click="goReportDetail(row.id)" />
              </el-tooltip>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </div>
  </div>
</template>

<style scoped>
.compat-run-page {
  flex: 1;
  height: 0;
  overflow: hidden;
  background: #f2f3f5;
}

.content-wrapper {
  height: calc(100% - 20px);
  margin: 10px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.config-card,
.reports-card {
  border-radius: 4px;
}

.config-card {
  flex-shrink: 0;
}

.reports-card {
  flex: 1;
  min-height: 260px;
  display: flex;
  flex-direction: column;
}

.reports-card :deep(.el-card__body) {
  flex: 1;
  min-height: 0;
}

.card-header,
.header-actions,
.page-set-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.card-header {
  justify-content: space-between;
}

.card-title {
  font-size: 15px;
  font-weight: 700;
  color: #303133;
}

.compat-form :deep(.el-form-item) {
  margin-bottom: 16px;
}

.compat-form :deep(.el-form-item__label) {
  padding-bottom: 4px;
  line-height: 18px;
  font-weight: 600;
  color: #606266;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 0 20px;
}

.threshold-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 0 16px;
  padding-top: 8px;
  border-top: 1px solid #ebeef5;
}

.wide-control,
.page-set-select {
  width: 100%;
}

.page-set-select {
  max-width: 560px;
}

.option-meta {
  float: right;
  margin-left: 16px;
  color: #909399;
  font-size: 12px;
}

.run-name {
  font-weight: 600;
  color: #303133;
}

.muted-text {
  color: #909399;
  font-size: 12px;
}

.summary-count {
  margin: 0 4px;
  font-weight: 700;
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
</style>
