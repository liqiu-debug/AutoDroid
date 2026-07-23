<script setup>
import { computed, onActivated, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Collection, Refresh, VideoPlay, View } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import api from '@/api'
import { runStatusTagType } from '@/utils/statusMeta'
import {
  compatibilityStatusLabel,
  compatibilityExecutionMode,
  normalizeReplayPreflight,
  packageSnapshotLabel,
  replayPathLabel,
  replayScopeLabel,
  resolveInspectionRunRouteSelection,
  sourceBoundaryEvidenceLabel,
  terminalOutcomeLabel,
} from '@/utils/compatibilityReplay'
import { useUserStore } from '@/stores/useUserStore'

const CURRENT_BASELINE_VALUE = '__current_installed__'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const packages = ref([])
const devices = ref([])
const environments = ref([])
const pageSets = ref([])
const recentRuns = ref([])
const inspectionRuns = ref([])
const branchOptions = ref([])
const replayPreflight = ref(null)
const selectedChainIds = ref([])
const routeSourceError = ref('')
const replayAdvancedGroups = ref([])
const loading = reactive({
  bootstrap: false,
  branches: false,
  preflight: false,
  reports: false,
  submitting: false,
  legacySubmitting: false,
})

const replayForm = reactive({
  name: '',
  inspection_run_id: '',
  branch_key: '',
  device_serial: '',
  duration_minutes: 60,
  manual_install_confirmed: false,
})

const legacyForm = reactive({
  name: '',
  source_type: 'page_set',
  inspection_run_id: '',
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

const replayEnabled = computed(() => userStore.featureFlags?.compatibility_installed_replay !== false)
const legacyCreationEnabled = computed(() => userStore.featureFlags?.compatibility_legacy_compare_creation === true)
const androidDevices = computed(() => (
  (devices.value || []).filter(item => (
    String(item.platform || 'android').toLowerCase() === 'android'
    && String(item.status || '').toUpperCase() !== 'OFFLINE'
  ))
))
const selectedInspectionRun = computed(() => (
  inspectionRuns.value.find(item => Number(item.id) === Number(replayForm.inspection_run_id)) || null
))
const replayChains = computed(() => replayPreflight.value?.chains || [])
const replayBlockers = computed(() => replayPreflight.value?.blockers || [])
const replayWarnings = computed(() => replayPreflight.value?.warnings || [])
const selectedChains = computed(() => new Set(selectedChainIds.value.map(String)))
const allChainsSelected = computed({
  get: () => replayChains.value.length > 0 && selectedChainIds.value.length === replayChains.value.length,
  set: selected => {
    selectedChainIds.value = selected ? replayChains.value.map(item => String(item.chain_id)) : []
  },
})
const replayCanSubmit = computed(() => (
  replayPreflight.value
  && !routeSourceError.value
  && replayBlockers.value.length === 0
  && replayPreflight.value.plan_digest
  && replayPreflight.value.device_snapshot_digest
  && selectedChainIds.value.length > 0
  && replayForm.manual_install_confirmed
))
const selectedSourceBlocked = computed(() => (
  selectedInspectionRun.value?.replay_source_eligible === false
))
const replayCanPreflight = computed(() => (
  replayForm.inspection_run_id
  && replayForm.branch_key
  && replayForm.device_serial
  && !routeSourceError.value
  && !selectedSourceBlocked.value
))

const isLegacyDeviceCompare = computed(() => legacyForm.compare_mode === 'device')
const isLegacyInspectionSource = computed(() => legacyForm.source_type === 'inspection')
const isLegacySnapshotCompare = computed(() => legacyForm.compare_mode === 'snapshot')
const requiresExplicitLegacyOld = computed(() => isLegacyInspectionSource.value && legacyForm.compare_mode === 'version')
const legacyCompareModeOptions = computed(() => {
  const options = [
    { label: '版本对比', value: 'version' },
    { label: '机型对比', value: 'device' },
  ]
  if (isLegacyInspectionSource.value) options.unshift({ label: '快照回归', value: 'snapshot' })
  return options
})
const isCurrentLegacyBaseline = computed(() => legacyForm.old_package_id === CURRENT_BASELINE_VALUE)
const legacyOldPackage = computed(() => (
  isCurrentLegacyBaseline.value ? null : packages.value.find(item => item.id === legacyForm.old_package_id)
))
const legacyNewPackageOptions = computed(() => {
  if (isLegacyDeviceCompare.value || isCurrentLegacyBaseline.value || !legacyOldPackage.value?.package_name) return packages.value
  return packages.value.filter(item => (
    item.package_name === legacyOldPackage.value.package_name && item.id !== legacyForm.old_package_id
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
const branchLabel = key => ({ guest: '未登录', authenticated: '已登录' }[key] || key || '-')
const evidenceLabel = value => ({
  VERIFIED_TWICE: '已复验',
  OBSERVED_ONCE: '已到达，待复验',
}[String(value || '').toUpperCase()] || value || '-')
const chainCheckpointLabel = chain => {
  return replayPathLabel(chain)
}
const replayRunSourceLabel = row => (
  row.inspection_run_id
    ? (row.inspection_run_name || row.page_set_name || '智能巡检报告')
    : '巡检来源已清理'
)
const recentRunModeLabel = row => (
  compatibilityExecutionMode(row) === 'installed_replay'
    ? '升级后链路回放'
    : ({ snapshot: '快照回归', version: '版本对比', device: '机型对比' }[row.compare_mode] || '兼容性对比')
)
const recentRunItemCount = row => (
  compatibilityExecutionMode(row) === 'installed_replay'
    ? (row.total_pages || row.total_chains || row.page_set?.pages?.length || 0)
    : (row.page_set?.pages?.length || row.total_pages || 0)
)

const errorMessage = (error, fallback) => {
  const detail = error.response?.data?.detail
  return typeof detail === 'string' ? detail : detail?.message || error.message || fallback
}

const fetchPackages = async () => {
  if (!legacyCreationEnabled.value) return
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
  if (!legacyCreationEnabled.value) return
  const { data } = await api.getEnvironments()
  environments.value = data || []
}
const fetchPageSets = async () => {
  if (!legacyCreationEnabled.value) return
  const { data } = await api.getCompatPageSets()
  pageSets.value = data || []
  if (!legacyForm.page_set_id && pageSets.value.length) legacyForm.page_set_id = pageSets.value[0].id
}
const fetchInspectionRuns = async () => {
  if (!userStore.featureFlags?.model_inspection) return
  const { data } = await api.getInspectionRuns({ page: 1, page_size: 100 })
  inspectionRuns.value = (data.items || []).filter(item => (
    ['PASS', 'WARNING', 'FAIL'].includes(statusText(item.status))
  ))
}

const loadRouteInspectionRun = async () => {
  const routeValue = route.query.inspection_run_id
  const hasRouteValue = routeValue !== undefined && routeValue !== null && String(routeValue).trim() !== ''
  if (!hasRouteValue) {
    routeSourceError.value = ''
    return false
  }

  const numericRunId = Number(routeValue)
  let routeRun = null
  let loadError = ''
  if (Number.isInteger(numericRunId) && numericRunId > 0) {
    try {
      const { data } = await api.getInspectionRun(numericRunId)
      routeRun = data || null
    } catch (error) {
      loadError = `无法加载巡检报告 #${numericRunId}：${errorMessage(error, '报告不存在或无权访问')}`
    }
  }

  const selection = resolveInspectionRunRouteSelection({
    routeValue,
    recentRuns: inspectionRuns.value,
    routeRun,
    loadError,
  })
  inspectionRuns.value = selection.options
  routeSourceError.value = selection.blocker
  replayForm.inspection_run_id = selection.selectionId
  if (selection.blocker) {
    replayForm.branch_key = ''
    resetPreflight()
  }
  return true
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

let branchRequestId = 0
const fetchBranches = async runId => {
  const numericRunId = Number(runId)
  branchOptions.value = []
  if (!numericRunId) return
  const requestId = ++branchRequestId
  loading.branches = true
  try {
    const run = inspectionRuns.value.find(item => Number(item.id) === numericRunId)
    const runBranches = Array.isArray(run?.branches)
      ? run.branches.map(item => item?.branch_key || item?.key).filter(Boolean)
      : Object.keys(run?.branches || {})
    let keys = [...new Set([
      ...(run?.selected_branches || []),
      ...runBranches,
    ].filter(Boolean))]
    if (!keys.length) {
      const { data } = await api.getInspectionGraph(numericRunId)
      keys = [...new Set((data.nodes || []).map(item => item.branch_key).filter(Boolean))]
    }
    if (requestId !== branchRequestId) return
    branchOptions.value = keys.map(key => ({ key, label: branchLabel(key) }))
    const queryBranch = String(route.query.branch_key || '')
    const preferred = [queryBranch, replayForm.branch_key, branchOptions.value[0]?.key]
      .find(key => key && branchOptions.value.some(item => item.key === key))
    replayForm.branch_key = preferred || ''
  } catch (error) {
    if (requestId === branchRequestId) ElMessage.error(errorMessage(error, '加载巡检业务线失败'))
  } finally {
    if (requestId === branchRequestId) loading.branches = false
  }
}

const refreshAll = async () => {
  loading.bootstrap = true
  try {
    await Promise.all([
      fetchDevices(),
      fetchInspectionRuns(),
      fetchRecentRuns(),
      fetchPackages(),
      fetchEnvironments(),
      fetchPageSets(),
    ])
    const hasExplicitRouteSource = await loadRouteInspectionRun()
    if (!hasExplicitRouteSource && !replayForm.inspection_run_id && inspectionRuns.value.length) {
      replayForm.inspection_run_id = inspectionRuns.value[0].id
    }
    if (replayForm.inspection_run_id) await fetchBranches(replayForm.inspection_run_id)
  } catch (error) {
    ElMessage.error(errorMessage(error, '加载兼容性测试数据失败'))
  } finally {
    loading.bootstrap = false
  }
}

const resetPreflight = () => {
  replayPreflight.value = null
  selectedChainIds.value = []
  replayForm.manual_install_confirmed = false
}

watch(() => replayForm.inspection_run_id, async (runId, previous) => {
  if (String(runId || '') === String(previous || '')) return
  resetPreflight()
  replayForm.branch_key = ''
  if (runId) await fetchBranches(runId)
})
watch(
  [() => replayForm.branch_key, () => replayForm.device_serial],
  ([branch, serial], [oldBranch, oldSerial]) => {
    if (branch !== oldBranch || serial !== oldSerial) resetPreflight()
  },
)

const runReplayPreflight = async () => {
  if (routeSourceError.value) return ElMessage.error(routeSourceError.value)
  if (!replayForm.inspection_run_id) return ElMessage.warning('请选择巡检来源报告')
  if (selectedSourceBlocked.value) {
    return ElMessage.warning(selectedInspectionRun.value?.replay_source_reason || '该巡检报告不能用于升级后回放')
  }
  if (!replayForm.branch_key) return ElMessage.warning('请选择一条业务线')
  if (!replayForm.device_serial) return ElMessage.warning('请选择已完成覆盖升级的设备')
  loading.preflight = true
  try {
    const { data } = await api.preflightCompatibilityReplay({
      inspection_run_id: Number(replayForm.inspection_run_id),
      branch_key: replayForm.branch_key,
      device_serial: replayForm.device_serial,
      max_chains: 20,
    })
    replayPreflight.value = normalizeReplayPreflight(data)
    selectedChainIds.value = replayPreflight.value.chains.map(item => String(item.chain_id))
    replayForm.manual_install_confirmed = false
    if (!replayForm.name.trim()) {
      const sourceName = selectedInspectionRun.value?.name || `巡检 #${replayForm.inspection_run_id}`
      const targetVersion = packageSnapshotLabel(replayPreflight.value.installed_package)
      replayForm.name = `${sourceName} · ${targetVersion} 回放`
    }
    if (replayBlockers.value.length) ElMessage.warning('预检存在阻断项，修复后请重新预检')
    else ElMessage.success(`预检完成，已规划 ${replayChains.value.length} 条链路`)
  } catch (error) {
    ElMessage.error(errorMessage(error, '回放预检失败'))
  } finally {
    loading.preflight = false
  }
}

const toggleChain = chainId => {
  const key = String(chainId)
  selectedChainIds.value = selectedChains.value.has(key)
    ? selectedChainIds.value.filter(item => String(item) !== key)
    : [...selectedChainIds.value, key]
}

const submitReplayRun = async () => {
  if (!replayCanSubmit.value) {
    if (replayBlockers.value.length) return ElMessage.warning('预检阻断项尚未解决')
    if (!selectedChainIds.value.length) return ElMessage.warning('至少保留一条回放链路')
    return ElMessage.warning('请确认设备已完成覆盖升级并保留原登录态')
  }
  loading.submitting = true
  try {
    const payload = {
      name: replayForm.name.trim() || `升级后链路回放 ${dayjs().format('MM-DD HH:mm')}`,
      execution_mode: 'installed_replay',
      source_type: 'inspection',
      inspection_run_id: Number(replayForm.inspection_run_id),
      replay_branch_key: replayForm.branch_key,
      device_serials: [replayForm.device_serial],
      selected_chain_ids: [...selectedChainIds.value],
      plan_digest: replayPreflight.value.plan_digest,
      device_snapshot_digest: replayPreflight.value.device_snapshot_digest,
      manual_install_confirmed: true,
      duration_seconds: Math.min(3600, Math.max(300, Number(replayForm.duration_minutes || 60) * 60)),
    }
    const { data } = await api.createCompatibilityRun(payload)
    ElMessage.success('升级后链路回放已提交')
    router.push(`/execution/reports/compatibility/${data.id}`)
  } catch (error) {
    ElMessage.error(errorMessage(error, '提交回放任务失败'))
  } finally {
    loading.submitting = false
  }
}

const buildLegacyRunName = () => {
  if (legacyForm.name) return
  const newPkg = packages.value.find(item => item.id === legacyForm.new_package_id)
  if (!newPkg) return
  if (isLegacyDeviceCompare.value) legacyForm.name = `机型对比 ${formatPackage(newPkg)}`
  else if (isLegacySnapshotCompare.value) legacyForm.name = `快照回归 ${formatPackage(newPkg)}`
  else legacyForm.name = `${formatPackage(legacyOldPackage.value) || '当前版本'} -> ${formatPackage(newPkg)}`
}
watch(() => legacyForm.new_package_id, buildLegacyRunName)
watch(() => legacyForm.source_type, source => {
  if (source === 'inspection') {
    legacyForm.compare_mode = 'snapshot'
    if (!legacyForm.inspection_run_id && inspectionRuns.value.length) legacyForm.inspection_run_id = inspectionRuns.value[0].id
  } else if (legacyForm.compare_mode === 'snapshot') legacyForm.compare_mode = 'version'
})
watch(() => legacyForm.compare_mode, mode => {
  if (mode === 'device') {
    legacyForm.mode = 'clean'
    legacyForm.baseline_device_serial ||= legacyForm.device_serials[0] || ''
  } else if (mode === 'version') legacyForm.mode = 'upgrade'
})

const submitLegacyRun = async () => {
  if (!legacyForm.new_package_id) return ElMessage.warning('请选择待测 APK')
  if (isLegacyInspectionSource.value && !legacyForm.inspection_run_id) return ElMessage.warning('请选择巡检来源任务')
  if (!isLegacyInspectionSource.value && !legacyForm.page_set_id) return ElMessage.warning('请选择页面合集')
  if (!legacyForm.device_serials.length) return ElMessage.warning('请选择测试设备')
  if (isLegacyDeviceCompare.value && legacyForm.device_serials.length < 2) return ElMessage.warning('机型对比至少选择两台设备')
  if (requiresExplicitLegacyOld.value && (!legacyForm.old_package_id || isCurrentLegacyBaseline.value)) return ElMessage.warning('请选择旧版 APK')
  loading.legacySubmitting = true
  try {
    const { data } = await api.createCompatibilityRun({
      name: legacyForm.name.trim() || `兼容性对比 ${dayjs().format('MM-DD HH:mm')}`,
      source_type: legacyForm.source_type,
      compare_mode: legacyForm.compare_mode,
      old_package_id: (isLegacyDeviceCompare.value || isLegacySnapshotCompare.value || isCurrentLegacyBaseline.value) ? null : legacyForm.old_package_id,
      new_package_id: legacyForm.new_package_id,
      page_set_id: isLegacyInspectionSource.value ? null : legacyForm.page_set_id,
      inspection_run_id: isLegacyInspectionSource.value ? legacyForm.inspection_run_id : null,
      inspection_state_ids: [],
      inspection_observation_ids: [],
      device_serials: legacyForm.device_serials,
      baseline_device_serial: isLegacyDeviceCompare.value ? legacyForm.baseline_device_serial : null,
      mode: legacyForm.mode,
      env_id: legacyForm.env_id || null,
      thresholds: {
        pixel_diff_ratio_warn: Number(legacyForm.pixel_diff_ratio_warn),
        ssim_warn: Number(legacyForm.ssim_warn),
        xml_diff_ratio_warn: Number(legacyForm.xml_diff_ratio_warn),
      },
    })
    ElMessage.success('兼容性对比任务已提交')
    router.push(`/execution/reports/compatibility/${data.id}`)
  } catch (error) {
    ElMessage.error(errorMessage(error, '提交失败'))
  } finally {
    loading.legacySubmitting = false
  }
}

const goPageSets = () => router.push('/special/compatibility/page-sets')
const goReports = () => router.push({ path: '/execution/reports', query: { tab: 'compatibility' } })
const goReportDetail = id => router.push(`/execution/reports/compatibility/${id}`)
const handleInspectionRunSelection = () => {
  routeSourceError.value = ''
  if (route.query.inspection_run_id === undefined) return
  const query = { ...route.query }
  delete query.inspection_run_id
  delete query.branch_key
  void router.replace({ query })
}

onMounted(refreshAll)
onActivated(refreshAll)
</script>

<template>
  <div class="compat-run-page" v-loading="loading.bootstrap">
    <div class="content-wrapper">
      <el-card v-if="replayEnabled" shadow="never" class="config-card">
        <template #header>
          <div class="card-header">
            <div>
              <div class="card-title">升级后链路回放</div>
              <div class="muted-text">在设备上手动覆盖升级后，复现历史巡检链路并验证页面可达性</div>
            </div>
            <div class="header-actions">
              <el-tooltip content="刷新设备和报告" placement="top">
                <el-button :icon="Refresh" circle aria-label="刷新设备和报告" @click="refreshAll" />
              </el-tooltip>
              <el-button type="primary" :loading="loading.preflight" :disabled="!replayCanPreflight" @click="runReplayPreflight">生成回放计划</el-button>
            </div>
          </div>
        </template>

        <el-alert
          title="使用设备当前已安装版本回放；系统不会安装、卸载或降级应用。"
          type="info"
          :closable="false"
          show-icon
          class="top-alert"
        />

        <el-alert
          v-if="routeSourceError"
          :title="routeSourceError"
          description="没有改用其他报告。请返回原报告重试，或在下方明确选择另一份报告。"
          type="error"
          :closable="false"
          show-icon
          class="top-alert"
        />

        <el-alert
          v-else-if="selectedSourceBlocked"
          :title="selectedInspectionRun?.replay_source_reason || '该巡检报告不能用于升级后回放'"
          type="warning"
          :closable="false"
          show-icon
          class="top-alert"
        />

        <el-form label-position="top" class="compat-form">
          <div class="form-grid replay-grid">
            <el-form-item label="巡检来源报告">
              <el-select v-model="replayForm.inspection_run_id" filterable placeholder="选择已完成的巡检报告" class="wide-control" @change="handleInspectionRunSelection">
                <el-option v-for="item in inspectionRuns" :key="item.id" :label="item.name" :value="item.id">
                  <span>{{ item.name }}</span>
                  <span class="option-meta">{{ compatibilityStatusLabel(item.status) }}</span>
                </el-option>
              </el-select>
            </el-form-item>
            <el-form-item label="业务线">
              <el-select v-model="replayForm.branch_key" :loading="loading.branches" placeholder="选择一条业务线" class="wide-control">
                <el-option v-for="item in branchOptions" :key="item.key" :label="item.label" :value="item.key" />
              </el-select>
            </el-form-item>
            <el-form-item label="已升级设备">
              <el-select v-model="replayForm.device_serial" filterable placeholder="选择一台 Android 设备" class="wide-control">
                <el-option
                  v-for="device in androidDevices"
                  :key="device.serial"
                  :label="formatDevice(device.serial)"
                  :value="device.serial"
                  :disabled="String(device.status).toUpperCase() !== 'IDLE'"
                >
                  <span>{{ formatDevice(device.serial) }}</span>
                  <span class="option-meta">{{ device.status === 'IDLE' ? '空闲' : '使用中' }}</span>
                </el-option>
              </el-select>
            </el-form-item>
          </div>
          <el-collapse v-model="replayAdvancedGroups" class="replay-advanced-collapse">
            <el-collapse-item title="更多设置" name="advanced">
              <div class="form-grid replay-options-grid">
                <el-form-item label="最长运行时间">
                  <el-input-number v-model="replayForm.duration_minutes" :min="5" :max="60" :step="5" controls-position="right" class="wide-control" />
                </el-form-item>
                <el-form-item label="任务名称">
                  <el-input v-model="replayForm.name" clearable placeholder="预检后自动生成" />
                </el-form-item>
              </div>
            </el-collapse-item>
          </el-collapse>
        </el-form>

        <section v-if="replayPreflight" class="preflight-panel">
          <div class="version-row">
            <div>
              <span>巡检来源版本</span>
              <strong>{{ packageSnapshotLabel(replayPreflight.source_package) }}</strong>
              <el-tag v-if="replayPreflight.source_package?.known === false" size="small" type="warning" effect="plain">未知</el-tag>
            </div>
            <div>
              <span>设备已安装版本</span>
              <strong>{{ packageSnapshotLabel(replayPreflight.installed_package) }}</strong>
              <el-tag :type="replayPreflight.installed_package?.installed === false ? 'danger' : 'success'" size="small" effect="plain">
                {{ replayPreflight.installed_package?.installed === false ? '未安装' : '已读取' }}
              </el-tag>
            </div>
            <div><span>计划</span><strong>{{ selectedChainIds.length }} / {{ replayChains.length }} 条链路</strong></div>
          </div>

          <el-alert
            v-for="item in replayBlockers"
            :key="`blocker-${item.code}-${item.message}`"
            :title="item.message || item.code"
            type="error"
            :closable="false"
            show-icon
          />
          <el-alert
            v-for="item in replayWarnings"
            :key="`warning-${item.code}-${item.message}`"
            :title="item.message || item.code"
            type="warning"
            :closable="false"
            show-icon
          />

          <div class="chain-toolbar">
            <div>
              <strong>回放链路</strong>
              <span class="muted-text">一条深层链路会同时验证沿途页面，可取消不需要的链路</span>
            </div>
            <el-checkbox v-model="allChainsSelected">全选</el-checkbox>
          </div>
          <el-table :data="replayChains" border max-height="360" row-key="chain_id" empty-text="没有可安全回放的链路">
            <el-table-column width="54" align="center">
              <template #default="{ row }">
                <el-checkbox :model-value="selectedChains.has(String(row.chain_id))" @change="toggleChain(row.chain_id)" />
              </template>
            </el-table-column>
            <el-table-column label="链路" min-width="220">
              <template #default="{ row }">
                <div class="run-name">{{ row.name }}</div>
                <div class="muted-text">{{ replayScopeLabel(row.replay_scope) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="经过页面" min-width="300">
              <template #default="{ row }">{{ chainCheckpointLabel(row) }}</template>
            </el-table-column>
            <el-table-column label="证据" width="120" align="center">
              <template #default="{ row }">
                <el-tag :type="row.evidence_level === 'VERIFIED_TWICE' ? 'success' : 'warning'" size="small" effect="plain">{{ evidenceLabel(row.evidence_level) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="终点" min-width="150" align="center">
              <template #default="{ row }">
                <el-tooltip
                  v-if="row.terminal_outcome && row.terminal_outcome !== 'NONE'"
                  :content="sourceBoundaryEvidenceLabel(row.source_boundary_evidence)"
                  placement="top"
                >
                  <el-tag size="small" effect="plain" type="info">{{ terminalOutcomeLabel(row.terminal_outcome) }}</el-tag>
                </el-tooltip>
                <span v-else class="muted-text">正常到达</span>
              </template>
            </el-table-column>
          </el-table>

          <el-checkbox v-model="replayForm.manual_install_confirmed" class="install-confirmation">
            我确认使用上述当前已安装版本执行回放，且需要保留的登录态和业务数据仍在
          </el-checkbox>
          <div class="preflight-actions">
            <el-button type="primary" :icon="VideoPlay" :loading="loading.submitting" :disabled="!replayCanSubmit" @click="submitReplayRun">开始回放</el-button>
          </div>
        </section>
      </el-card>

      <el-alert
        v-else
        title="升级后链路回放当前未启用"
        type="warning"
        :closable="false"
        show-icon
      />

      <el-collapse v-if="legacyCreationEnabled" class="legacy-collapse">
        <el-collapse-item title="高级：旧版视觉 / XML 对比任务" name="legacy">
          <el-form label-position="top" class="compat-form legacy-form">
            <div class="form-grid">
              <el-form-item label="任务名称"><el-input v-model="legacyForm.name" clearable /></el-form-item>
              <el-form-item label="页面来源">
                <el-segmented v-model="legacyForm.source_type" :options="[{ label: '人工页面合集', value: 'page_set' }, { label: '巡检快照', value: 'inspection' }]" />
              </el-form-item>
              <el-form-item label="对比维度"><el-segmented v-model="legacyForm.compare_mode" :options="legacyCompareModeOptions" /></el-form-item>
              <el-form-item v-if="!isLegacySnapshotCompare" label="执行模式"><el-segmented v-model="legacyForm.mode" :options="[{ label: '升级兼容', value: 'upgrade' }, { label: '干净对比', value: 'clean' }]" /></el-form-item>
              <el-form-item label="变量环境">
                <el-select v-model="legacyForm.env_id" clearable filterable placeholder="不使用环境变量"><el-option v-for="env in environments" :key="env.id" :label="env.name" :value="env.id" /></el-select>
              </el-form-item>
            </div>
            <div class="form-grid">
              <el-form-item v-if="!isLegacyDeviceCompare && !isLegacySnapshotCompare" label="旧版 APK">
                <el-select v-model="legacyForm.old_package_id" filterable class="wide-control">
                  <el-option v-if="!requiresExplicitLegacyOld" label="当前版本" :value="CURRENT_BASELINE_VALUE" />
                  <el-option v-for="pkg in packages" :key="pkg.id" :label="formatPackage(pkg)" :value="pkg.id" />
                </el-select>
              </el-form-item>
              <el-form-item label="待测 APK">
                <el-select v-model="legacyForm.new_package_id" filterable class="wide-control"><el-option v-for="pkg in legacyNewPackageOptions" :key="pkg.id" :label="formatPackage(pkg)" :value="pkg.id" /></el-select>
              </el-form-item>
              <el-form-item label="测试设备">
                <el-select v-model="legacyForm.device_serials" multiple filterable collapse-tags class="wide-control">
                  <el-option v-for="device in androidDevices" :key="device.serial" :label="formatDevice(device.serial)" :value="device.serial" :disabled="String(device.status).toUpperCase() !== 'IDLE'" />
                </el-select>
              </el-form-item>
              <el-form-item v-if="isLegacyDeviceCompare" label="基准设备">
                <el-select v-model="legacyForm.baseline_device_serial" class="wide-control"><el-option v-for="serial in legacyForm.device_serials" :key="serial" :label="formatDevice(serial)" :value="serial" /></el-select>
              </el-form-item>
            </div>
            <el-form-item v-if="!isLegacyInspectionSource" label="页面合集">
              <div class="page-set-row"><el-select v-model="legacyForm.page_set_id" class="page-set-select"><el-option v-for="set in pageSets" :key="set.id" :label="`${set.name} (${set.pages?.length || 0})`" :value="set.id" /></el-select><el-button :icon="Collection" @click="goPageSets">管理合集</el-button></div>
            </el-form-item>
            <el-form-item v-else label="巡检来源任务">
              <el-select v-model="legacyForm.inspection_run_id" filterable class="wide-control"><el-option v-for="item in inspectionRuns" :key="item.id" :label="item.name" :value="item.id" /></el-select>
            </el-form-item>
            <div class="threshold-grid">
              <el-form-item label="像素差异"><el-input-number v-model="legacyForm.pixel_diff_ratio_warn" :min="0" :max="1" :step="0.01" /></el-form-item>
              <el-form-item label="SSIM"><el-input-number v-model="legacyForm.ssim_warn" :min="0" :max="1" :step="0.01" /></el-form-item>
              <el-form-item label="XML 差异"><el-input-number v-model="legacyForm.xml_diff_ratio_warn" :min="0" :max="1" :step="0.05" /></el-form-item>
            </div>
            <div class="legacy-actions"><el-button type="primary" :loading="loading.legacySubmitting" @click="submitLegacyRun">开始高级对比</el-button></div>
          </el-form>
        </el-collapse-item>
      </el-collapse>

      <el-card shadow="never" class="reports-card">
        <template #header>
          <div class="card-header">
            <span class="card-title">最新兼容性报告</span>
            <div class="header-actions"><el-button link type="primary" @click="goReports">前往报告中心</el-button><el-button :icon="Refresh" @click="fetchRecentRuns">刷新</el-button></div>
          </div>
        </template>
        <el-table :data="recentRuns" v-loading="loading.reports" height="100%" row-key="id">
          <el-table-column label="任务" min-width="220"><template #default="{ row }"><div class="run-name">{{ row.name }}</div><div class="muted-text">{{ row.package_name }}</div></template></el-table-column>
          <el-table-column label="来源 / 模式" min-width="190"><template #default="{ row }"><div>{{ row.source_type === 'inspection' ? replayRunSourceLabel(row) : (row.page_set?.name || '-') }}</div><div class="muted-text">{{ recentRunModeLabel(row) }}</div></template></el-table-column>
          <el-table-column label="设备 / 链路" width="100" align="center"><template #default="{ row }">{{ row.total_cells }} / {{ recentRunItemCount(row) }}</template></el-table-column>
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
.compat-run-page { flex: 1; height: 0; overflow: auto; background: #f2f3f5; }
.content-wrapper { min-height: calc(100% - 20px); margin: 10px; display: flex; flex-direction: column; gap: 12px; }
.config-card, .reports-card, .legacy-collapse { border-radius: 4px; }
.reports-card { min-height: 300px; }
.card-header, .header-actions, .page-set-row, .chain-toolbar, .version-row { display: flex; align-items: center; gap: 12px; }
.card-header, .chain-toolbar { justify-content: space-between; }
.card-title { font-size: 15px; font-weight: 700; color: #303133; }
.top-alert { margin-bottom: 16px; }
.compat-form :deep(.el-form-item) { margin-bottom: 16px; }
.compat-form :deep(.el-form-item__label) { padding-bottom: 4px; line-height: 18px; font-weight: 600; color: #606266; }
.form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 0 20px; }
.replay-grid { grid-template-columns: repeat(3, minmax(180px, 1fr)); }
.replay-options-grid { grid-template-columns: repeat(2, minmax(220px, 1fr)); }
.replay-advanced-collapse { margin-top: -6px; border-top: 0; }
.replay-advanced-collapse :deep(.el-collapse-item__header) { height: 38px; color: #606266; font-size: 13px; }
.replay-advanced-collapse :deep(.el-collapse-item__wrap) { border-bottom: 0; }
.replay-advanced-collapse :deep(.el-collapse-item__content) { padding: 8px 0 0; }
.threshold-grid { display: grid; grid-template-columns: repeat(3, minmax(180px, 1fr)); gap: 0 16px; padding-top: 8px; border-top: 1px solid #ebeef5; }
.wide-control, .page-set-select { width: 100%; }
.page-set-select { max-width: 560px; }
.option-meta { float: right; margin-left: 16px; color: #909399; font-size: 12px; }
.preflight-panel { display: flex; flex-direction: column; gap: 10px; padding-top: 14px; border-top: 1px solid #ebeef5; }
.version-row { align-items: stretch; flex-wrap: wrap; }
.version-row > div { min-width: 240px; padding: 10px 12px; border: 1px solid #ebeef5; border-radius: 4px; background: #fafafa; display: flex; align-items: center; gap: 8px; }
.version-row span { color: #909399; font-size: 12px; }
.chain-toolbar { margin-top: 4px; }
.chain-toolbar > div { display: flex; align-items: baseline; gap: 10px; }
.install-confirmation { margin-top: 4px; }
.preflight-actions { display: flex; justify-content: flex-end; }
.legacy-collapse { padding: 0 16px; border: 1px solid #dcdfe6; background: #fff; }
.legacy-form { padding: 4px 0 12px; }
.legacy-actions { display: flex; justify-content: flex-end; }
.run-name { font-weight: 600; color: #303133; }
.muted-text { color: #909399; font-size: 12px; }
.result-summary { display: inline-flex; align-items: center; justify-content: center; gap: 8px; white-space: nowrap; }
.summary-count { font-size: 12px; font-weight: 600; }
.pass { color: #67c23a; } .warn { color: #e6a23c; } .fail { color: #f56c6c; }
@media (max-width: 960px) {
  .replay-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .card-header { align-items: flex-start; flex-direction: column; }
  .header-actions { width: 100%; flex-wrap: wrap; }
}
@media (max-width: 600px) {
  .replay-grid, .replay-options-grid, .form-grid, .threshold-grid { grid-template-columns: 1fr; }
  .version-row > div { width: 100%; min-width: 0; }
  .chain-toolbar { align-items: flex-start; }
  .chain-toolbar > div { flex-direction: column; gap: 2px; }
}
</style>
