<script setup>
import { computed, onActivated, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Edit, Plus, Refresh, VideoPlay } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import api from '@/api'
import { runStatusTagType } from '@/utils/statusMeta'
import {
  inspectionRunCoverage,
  inspectionRunReplay,
  inspectionRunStatusLabel,
} from '@/utils/inspectionRunPresentation'

const router = useRouter()
const profiles = ref([])
const cases = ref([])
const environments = ref([])
const devices = ref([])
const packages = ref([])
const runs = ref([])
const loading = reactive({ page: false, profiles: false, runs: false, submit: false, save: false })
const dialogVisible = ref(false)
const editingId = ref(null)
const runAdvancedOpen = ref([])
const profileManagerOpen = ref([])
let pollTimer = null

const blankBranch = (name, scope = 'full') => ({
  name,
  prepare_case_id: '',
  entry_case_id: '',
  env_id: null,
  ready_assertion: { by: 'description', selector: '', timeout: 5 },
  scope,
})

const RESERVED_BRANCH_KEYS = ['guest', 'authenticated']

const profileForm = reactive({
  name: '',
  package_name: '',
  branches: {
    guest: blankBranch('未登录'),
    authenticated: blankBranch('已登录'),
  },
  dynamic_text_patterns_text: '[]',
  input_rules_text: '[]',
  safety_rules_text: '[]',
  sanitizer_rules_text: '[]',
  duration_seconds: 1800,
  max_states: 200,
  max_device_actions: 800,
  max_depth: 12,
  max_scrolls_per_direction: 3,
  max_coverage_scroll_actions: 50,
  max_variants_per_cluster: 5,
  no_new_coverage_limit: 100,
  max_observations: 400,
  max_artifact_mib: 512,
  stable_wait_seconds: 5,
  enable_performance_monitor: true,
  enable_jank_frame_monitor: false,
  enable_perfetto_trace: false,
  enable_local_replay: true,
  capture_log: true,
})

const runForm = reactive({
  profile_id: '',
  name: '',
  device_serial: '',
  package_id: null,
  branches: ['guest', 'authenticated'],
  duration_minutes: 30,
})

const durationPresetOptions = [
  { label: '30 分', value: '30' },
  { label: '45 分', value: '45' },
  { label: '60 分', value: '60' },
  { label: '90 分', value: '90' },
  { label: '120 分', value: '120' },
  { label: '自定义', value: 'custom' },
]

const androidDevices = computed(() => devices.value.filter(item => (
  String(item.platform || 'android').toLowerCase() === 'android'
)))
const hasRunning = computed(() => runs.value.some(item => ['PENDING', 'RUNNING', 'QUEUED'].includes(String(item.status).toUpperCase())))
const selectedProfile = computed(() => profiles.value.find(item => item.id === runForm.profile_id))
const normalizedRunDurationMinutes = computed(() => Math.min(120, Math.max(5, Number(runForm.duration_minutes) || 30)))
const runDurationPreset = computed({
  get: () => {
    const minutes = normalizedRunDurationMinutes.value
    return [30, 45, 60, 90, 120].includes(minutes) ? String(minutes) : 'custom'
  },
  set: (value) => {
    runForm.duration_minutes = value === 'custom' ? 75 : Number(value)
  },
})
const runDurationAllocation = computed(() => {
  const total = normalizedRunDurationMinutes.value
  const exploration = Math.max(1, Math.floor(total * 0.85))
  return `${exploration} 分钟探索 · ${Math.max(1, total - exploration)} 分钟验证`
})
const packageOptions = computed(() => packages.value.filter(item => (
  !selectedProfile.value?.package_name
  || !item.package_name
  || item.package_name === selectedProfile.value.package_name
)))

const profileBranchKeys = computed(() => {
  const keys = Object.keys(profileForm.branches || {})
  return [
    ...RESERVED_BRANCH_KEYS.filter(key => keys.includes(key)),
    ...keys.filter(key => !RESERVED_BRANCH_KEYS.includes(key)),
  ]
})

const availableRunBranches = computed(() => {
  const profile = selectedProfile.value
  if (!profile) {
    return [
      { key: 'guest', name: '未登录', scope: 'full' },
      { key: 'authenticated', name: '已登录', scope: 'full' },
    ]
  }
  return Object.entries(profile.branches || {}).map(([key, config]) => ({
    key,
    name: config?.name || key,
    scope: config?.scope || 'full',
  }))
})

const addPageBranch = async () => {
  let value
  try {
    ({ value } = await ElMessageBox.prompt(
      '输入业务线标识（小写字母/数字，可含 _ 和 -）。该业务线将以“单页巡检”模式执行：进入用例到达的页面会被穷举，跳出该页面的入口只记录去向。',
      '新增单页业务线',
      {
        inputPattern: /^[a-z0-9][a-z0-9_-]{0,63}$/,
        inputErrorMessage: '仅支持小写字母、数字、_ 和 -，且以字母或数字开头',
      },
    ))
  } catch {
    return
  }
  if (!value) return
  if (profileForm.branches[value]) {
    ElMessage.warning('该业务线标识已存在')
    return
  }
  profileForm.branches[value] = blankBranch(value, 'single_page')
}

const removePageBranch = (key) => {
  if (RESERVED_BRANCH_KEYS.includes(key)) return
  delete profileForm.branches[key]
}
const runAdvancedSummary = computed(() => {
  const selectedPackage = packageOptions.value.find(item => item.id === runForm.package_id)
  const labels = []
  if (runForm.name.trim()) labels.push(runForm.name.trim())
  if (selectedPackage) labels.push(selectedPackage.version_name || selectedPackage.version_code || selectedPackage.app_name || selectedPackage.package_name)
  return labels.length ? labels.join(' · ') : '任务名称、安装包与安全说明'
})

watch(selectedProfile, (profile) => {
  if (!profile) return
  const seconds = Number(profile.budgets?.duration_seconds || 1800)
  runForm.duration_minutes = Math.min(120, Math.max(5, Math.round(seconds / 60)))
  const keys = Object.keys(profile.branches || {})
  runForm.branches = keys.length ? keys : ['guest', 'authenticated']
})

const parseJsonArray = (text, label) => {
  let value
  try {
    value = JSON.parse(text || '[]')
  } catch (error) {
    throw new Error(`${label}不是合法 JSON：${error.message}`)
  }
  if (!Array.isArray(value)) throw new Error(`${label}必须是 JSON 数组`)
  return value
}

const resetProfile = () => {
  editingId.value = null
  Object.assign(profileForm, {
    name: '',
    package_name: '',
    branches: {
      guest: blankBranch('未登录'),
      authenticated: blankBranch('已登录'),
    },
    dynamic_text_patterns_text: '[]',
    input_rules_text: '[]',
    safety_rules_text: '[]',
    sanitizer_rules_text: '[]',
    duration_seconds: 1800,
    max_states: 200,
    max_device_actions: 800,
    max_depth: 12,
    max_scrolls_per_direction: 3,
    max_coverage_scroll_actions: 50,
    max_variants_per_cluster: 5,
    no_new_coverage_limit: 100,
    max_observations: 400,
    max_artifact_mib: 512,
    stable_wait_seconds: 5,
    enable_performance_monitor: true,
    enable_jank_frame_monitor: false,
    enable_perfetto_trace: false,
    enable_local_replay: true,
    capture_log: true,
  })
}

const openCreate = () => {
  resetProfile()
  dialogVisible.value = true
}

const openEdit = (profile) => {
  resetProfile()
  editingId.value = profile.id
  profileForm.name = profile.name
  profileForm.package_name = profile.package_name
  profileForm.branches = JSON.parse(JSON.stringify(profile.branches))
  for (const branch of Object.values(profileForm.branches)) {
    if (!branch.scope) branch.scope = 'full'
    if (!branch.ready_assertion) branch.ready_assertion = { by: 'description', selector: '', timeout: 5 }
  }
  profileForm.dynamic_text_patterns_text = JSON.stringify(profile.dynamic_text_patterns || [], null, 2)
  profileForm.input_rules_text = JSON.stringify(profile.input_rules || [], null, 2)
  profileForm.safety_rules_text = JSON.stringify(profile.safety_rules || [], null, 2)
  profileForm.sanitizer_rules_text = JSON.stringify(profile.sanitizer_rules || [], null, 2)
  const budgets = profile.budgets || {}
  Object.assign(profileForm, budgets, profile.monitor_options || {})
  profileForm.max_device_actions = budgets.max_device_actions ?? budgets.max_actions ?? 800
  profileForm.max_coverage_scroll_actions = budgets.max_coverage_scroll_actions ?? 50
  profileForm.no_new_coverage_limit = budgets.no_new_coverage_limit ?? budgets.no_new_state_limit ?? 100
  profileForm.max_observations = budgets.max_observations ?? 400
  profileForm.max_artifact_mib = Math.max(1, Math.round(Number(budgets.max_artifact_bytes ?? 512 * 1024 * 1024) / 1024 / 1024))
  dialogVisible.value = true
}

const profilePayload = () => ({
  name: profileForm.name.trim(),
  package_name: profileForm.package_name.trim(),
  branches: JSON.parse(JSON.stringify(profileForm.branches)),
  dynamic_text_patterns: parseJsonArray(profileForm.dynamic_text_patterns_text, '动态文案规则'),
  input_rules: parseJsonArray(profileForm.input_rules_text, '输入规则'),
  safety_rules: parseJsonArray(profileForm.safety_rules_text, '安全规则'),
  sanitizer_rules: parseJsonArray(profileForm.sanitizer_rules_text, '脱敏规则'),
  budgets: {
    duration_seconds: Number(profileForm.duration_seconds),
    max_states: Number(profileForm.max_states),
    max_device_actions: Number(profileForm.max_device_actions),
    max_depth: Number(profileForm.max_depth),
    max_scrolls_per_direction: Number(profileForm.max_scrolls_per_direction),
    max_coverage_scroll_actions: Number(profileForm.max_coverage_scroll_actions),
    max_variants_per_cluster: Number(profileForm.max_variants_per_cluster),
    no_new_coverage_limit: Number(profileForm.no_new_coverage_limit),
    max_observations: Number(profileForm.max_observations),
    max_artifact_bytes: Number(profileForm.max_artifact_mib) * 1024 * 1024,
    stable_wait_seconds: Number(profileForm.stable_wait_seconds),
  },
  monitor_options: {
    enable_performance_monitor: profileForm.enable_performance_monitor,
    enable_jank_frame_monitor: profileForm.enable_jank_frame_monitor,
    enable_perfetto_trace: profileForm.enable_perfetto_trace,
    enable_local_replay: profileForm.enable_local_replay,
    capture_log: profileForm.capture_log,
  },
})

const saveProfile = async () => {
  if (!profileForm.name.trim() || !profileForm.package_name.trim()) {
    return ElMessage.warning('请填写配置名称和目标包名')
  }
  for (const key of profileBranchKeys.value) {
    const branch = profileForm.branches[key]
    if (!branch.prepare_case_id || !branch.entry_case_id || !branch.ready_assertion?.selector?.trim()) {
      return ElMessage.warning(`请完整配置${branch.name || key}业务线`)
    }
  }
  loading.save = true
  try {
    const payload = profilePayload()
    if (editingId.value) await api.updateInspectionProfile(editingId.value, payload)
    else await api.createInspectionProfile(payload)
    ElMessage.success(editingId.value ? '巡检配置已更新' : '巡检配置已创建')
    dialogVisible.value = false
    await fetchProfiles()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || '保存失败')
  } finally {
    loading.save = false
  }
}

const removeProfile = async (profile) => {
  try {
    await ElMessageBox.confirm(`删除巡检配置“${profile.name}”？历史报告不会受影响。`, '删除确认', { type: 'warning' })
    await api.deleteInspectionProfile(profile.id)
    ElMessage.success('已删除')
    await fetchProfiles()
  } catch (error) {
    if (error !== 'cancel') ElMessage.error(error.response?.data?.detail || error.message || '删除失败')
  }
}

const fetchProfiles = async () => {
  loading.profiles = true
  try {
    const { data } = await api.getInspectionProfiles()
    profiles.value = data || []
    if (!runForm.profile_id && profiles.value.length) runForm.profile_id = profiles.value[0].id
  } finally {
    loading.profiles = false
  }
}

const fetchRuns = async () => {
  loading.runs = true
  try {
    const { data } = await api.getInspectionRuns({ page: 1, page_size: 10 })
    runs.value = data.items || []
  } finally {
    loading.runs = false
  }
}

const fetchAndroidPackages = async () => {
  const all = []
  const pageSize = 100
  for (let page = 1; page <= 20; page += 1) {
    const { data } = await api.getPackages({
      page,
      page_size: pageSize,
      platform: 'android',
    })
    const items = data.items || []
    all.push(...items)
    if (all.length >= Number(data.total ?? all.length) || items.length < pageSize) break
  }
  return all
}

const bootstrap = async () => {
  loading.page = true
  try {
    const [caseResponse, envResponse, deviceResponse, packageResponse] = await Promise.all([
      api.getTestCases({ skip: 0, limit: 1000 }),
      api.getEnvironments(),
      api.getDeviceList(),
      fetchAndroidPackages(),
    ])
    cases.value = caseResponse.data?.items || caseResponse.data || []
    environments.value = envResponse.data || []
    devices.value = deviceResponse.data || []
    packages.value = packageResponse || []
    await Promise.all([fetchProfiles(), fetchRuns()])
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || '加载巡检数据失败')
  } finally {
    loading.page = false
  }
}

const startRun = async () => {
  if (!runForm.profile_id || !runForm.device_serial || !runForm.branches.length) {
    return ElMessage.warning('请选择巡检配置、显式设备和至少一条业务线')
  }
  loading.submit = true
  try {
    const profile = selectedProfile.value
    const { data } = await api.createInspectionRun({
      profile_id: runForm.profile_id,
      name: runForm.name.trim() || `${profile?.name || '智能巡检'} ${dayjs().format('MM-DD HH:mm')}`,
      device_serial: runForm.device_serial,
      package_id: runForm.package_id || null,
      branches: runForm.branches,
      duration_seconds: normalizedRunDurationMinutes.value * 60,
    })
    ElMessage.success('智能巡检任务已提交')
    router.push(`/execution/reports/inspection/${data.id}`)
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || '任务提交失败')
  } finally {
    loading.submit = false
  }
}

const cancelRun = async (run) => {
  try {
    await api.cancelInspectionRun(run.id)
    ElMessage.success('已请求取消')
    await fetchRuns()
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || error.message || '取消失败')
  }
}

const statusText = status => String(status || 'PENDING').toUpperCase()
const formatTime = value => value ? dayjs(value).format('MM-DD HH:mm:ss') : '-'

onMounted(() => {
  bootstrap()
  pollTimer = window.setInterval(() => {
    if (hasRunning.value) fetchRuns()
  }, 5000)
})
onActivated(() => Promise.all([fetchProfiles(), fetchRuns()]))
onBeforeUnmount(() => {
  if (pollTimer) window.clearInterval(pollTimer)
})
</script>

<template>
  <div class="inspection-page" v-loading="loading.page">
    <div class="content">
      <el-card shadow="never">
        <template #header>
          <div class="header">
            <div>
              <div class="title">智能巡检</div>
            </div>
            <div class="actions">
              <el-tooltip content="刷新配置、设备和任务" placement="top">
                <el-button :icon="Refresh" circle @click="bootstrap" />
              </el-tooltip>
            </div>
          </div>
        </template>

        <el-form label-position="top" class="launch-form">
          <div class="run-grid">
            <el-form-item label="巡检配置">
              <el-select v-model="runForm.profile_id" filterable placeholder="选择配置">
                <el-option v-for="item in profiles" :key="item.id" :label="`${item.name} · ${item.package_name}`" :value="item.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="运行设备">
              <el-select v-model="runForm.device_serial" filterable placeholder="仅可选择空闲 Android 设备">
                <el-option
                  v-for="item in androidDevices"
                  :key="item.serial"
                  :label="`${item.custom_name || item.market_name || item.model || item.serial} · ${item.status}`"
                  :value="item.serial"
                  :disabled="String(item.status).toUpperCase() !== 'IDLE'"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="业务线">
              <el-checkbox-group v-model="runForm.branches">
                <el-checkbox v-for="item in availableRunBranches" :key="item.key" :value="item.key">
                  {{ item.name }}<template v-if="item.scope === 'single_page'">（单页）</template>
                </el-checkbox>
              </el-checkbox-group>
            </el-form-item>
            <el-form-item label="本次运行上限">
              <div class="duration-control">
                <el-segmented class="duration-presets" v-model="runDurationPreset" :options="durationPresetOptions" size="small" />
                <el-select class="duration-preset-select" v-model="runDurationPreset">
                  <el-option v-for="option in durationPresetOptions" :key="option.value" :label="option.label" :value="option.value" />
                </el-select>
                <div v-if="runDurationPreset === 'custom'" class="duration-custom">
                  <el-input-number v-model="runForm.duration_minutes" :min="5" :max="120" controls-position="right" />
                  <span>分钟</span>
                </div>
                <span class="duration-allocation">{{ runDurationAllocation }}</span>
              </div>
            </el-form-item>
            <div class="start-run-control">
              <el-tooltip content="支付、删除等危险操作会在执行前拦截，拦截前已走通的页面仍可安全回放。" placement="top">
                <el-button type="primary" :icon="VideoPlay" :loading="loading.submit" @click="startRun">开始巡检</el-button>
              </el-tooltip>
            </div>
          </div>
          <el-collapse v-model="runAdvancedOpen" class="launch-more">
            <el-collapse-item name="more">
              <template #title>
                <div class="launch-more-title">
                  <strong>更多选项</strong>
                  <span>{{ runAdvancedSummary }}</span>
                </div>
              </template>
              <div class="run-secondary-grid">
                <el-form-item label="任务名称（可选）">
                  <el-input v-model="runForm.name" clearable placeholder="未填写时自动生成" />
                </el-form-item>
                <el-form-item label="安装包（可选）">
                  <el-select v-model="runForm.package_id" clearable filterable placeholder="使用设备当前已安装版本">
                    <el-option
                      v-for="item in packageOptions"
                      :key="item.id"
                      :label="`${item.app_name || item.package_name} ${item.version_name || item.version_code || ''}`"
                      :value="item.id"
                    />
                  </el-select>
                </el-form-item>
              </div>
              <div class="safety-note">支付、删除等危险操作会在执行前拦截；拦截前已到达的页面仍可用于安全回放。</div>
            </el-collapse-item>
          </el-collapse>
        </el-form>
      </el-card>

      <div class="workspace-sections">
        <el-card shadow="never" class="recent-panel">
          <template #header>
            <div class="header">
              <span class="title small">最近任务</span>
              <el-button link type="primary" :icon="Refresh" @click="fetchRuns">刷新</el-button>
            </div>
          </template>
          <el-table
            :data="runs"
            v-loading="loading.runs"
            max-height="300"
            row-key="id"
            class="recent-runs-table"
            @row-click="row => router.push(`/execution/reports/inspection/${row.id}`)"
          >
            <el-table-column label="任务" min-width="190">
              <template #default="{ row }">
                <div class="strong">{{ row.name }}</div>
                <div class="subtitle">{{ formatTime(row.started_at || row.created_at) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="覆盖" min-width="140">
              <template #default="{ row }">
                <div class="run-metric">
                  <strong>{{ inspectionRunCoverage(row).label }}</strong>
                  <span>{{ inspectionRunCoverage(row).detail }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="可回放" min-width="140">
              <template #default="{ row }">
                <div class="run-metric">
                  <strong>{{ inspectionRunReplay(row).label }}</strong>
                  <span>{{ inspectionRunReplay(row).detail }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="96">
              <template #default="{ row }">
                <div class="run-status-cell">
                  <el-tag :type="runStatusTagType(row.status)" size="small" effect="plain">{{ inspectionRunStatusLabel(row) }}</el-tag>
                  <el-button
                    v-if="['PENDING', 'RUNNING', 'QUEUED'].includes(statusText(row.status))"
                    link
                    type="danger"
                    @click.stop="cancelRun(row)"
                  >取消</el-button>
                </div>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-collapse v-model="profileManagerOpen" class="profile-manager">
          <el-collapse-item name="profiles">
            <template #title>
              <div class="profile-manager-title">
                <div>
                  <strong>配置管理</strong>
                  <span>{{ profiles.length }} 个巡检配置</span>
                </div>
                <el-button link type="primary" :icon="Plus" @click.stop="openCreate">新建配置</el-button>
              </div>
            </template>
            <el-table :data="profiles" v-loading="loading.profiles" max-height="320" row-key="id">
              <el-table-column label="配置" min-width="210">
                <template #default="{ row }">
                  <div class="strong">{{ row.name }}</div>
                  <div class="subtitle">{{ row.package_name }}</div>
                </template>
              </el-table-column>
              <el-table-column label="默认预算" width="150">
                <template #default="{ row }">{{ Math.round((row.budgets?.duration_seconds || 0) / 60) }} 分 · {{ row.budgets?.max_states }} 页</template>
              </el-table-column>
              <el-table-column label="操作" width="92" align="right">
                <template #default="{ row }">
                  <el-button :icon="Edit" link @click="openEdit(row)" />
                  <el-button :icon="Delete" link type="danger" @click="removeProfile(row)" />
                </template>
              </el-table-column>
            </el-table>
          </el-collapse-item>
        </el-collapse>
      </div>
    </div>

    <el-dialog v-model="dialogVisible" :title="editingId ? '编辑巡检配置' : '新建巡检配置'" width="900px" class="inspection-profile-dialog" align-center destroy-on-close>
      <div class="profile-dialog-scroll">
      <el-form label-position="top">
        <div class="dialog-grid">
          <el-form-item label="配置名称"><el-input v-model="profileForm.name" /></el-form-item>
          <el-form-item label="目标应用包名"><el-input v-model="profileForm.package_name" /></el-form-item>
        </div>

        <el-tabs>
          <el-tab-pane v-for="branchKey in profileBranchKeys" :key="branchKey" :label="profileForm.branches[branchKey].name || branchKey">
            <div class="dialog-grid">
              <el-form-item label="业务线名称" v-if="!RESERVED_BRANCH_KEYS.includes(branchKey)">
                <el-input v-model="profileForm.branches[branchKey].name" placeholder="例如：我的页面" />
              </el-form-item>
              <el-form-item label="巡检范围">
                <el-select v-model="profileForm.branches[branchKey].scope">
                  <el-option label="整应用探索（BFS）" value="full" />
                  <el-option label="仅入口单页穷举" value="single_page" />
                </el-select>
              </el-form-item>
              <el-form-item label="准备用例">
                <el-select v-model="profileForm.branches[branchKey].prepare_case_id" filterable>
                  <el-option v-for="item in cases" :key="item.id" :label="item.name" :value="item.id" />
                </el-select>
              </el-form-item>
              <el-form-item label="进入用例">
                <el-select v-model="profileForm.branches[branchKey].entry_case_id" filterable>
                  <el-option v-for="item in cases" :key="item.id" :label="item.name" :value="item.id" />
                </el-select>
              </el-form-item>
              <el-form-item label="变量环境">
                <el-select v-model="profileForm.branches[branchKey].env_id" clearable filterable>
                  <el-option v-for="item in environments" :key="item.id" :label="item.name" :value="item.id" />
                </el-select>
              </el-form-item>
              <el-form-item label="就绪断言定位方式">
                <el-select v-model="profileForm.branches[branchKey].ready_assertion.by">
                  <el-option label="description（优先）" value="description" />
                  <el-option label="text" value="text" />
                  <el-option label="XPath" value="xpath" />
                </el-select>
              </el-form-item>
              <el-form-item label="就绪断言 selector">
                <el-input v-model="profileForm.branches[branchKey].ready_assertion.selector" />
              </el-form-item>
              <el-form-item label="就绪超时（秒）">
                <el-input-number v-model="profileForm.branches[branchKey].ready_assertion.timeout" :min="1" :max="60" />
              </el-form-item>
            </div>
            <div v-if="profileForm.branches[branchKey].scope === 'single_page'" class="scope-note">
              单页模式：进入用例到达的页面（含滚动视口和弹层）会被穷举；跳出该页面的控件只记录去向，报告会列出这些“已发现未配置”的页面。
            </div>
            <div v-if="!RESERVED_BRANCH_KEYS.includes(branchKey)" class="branch-actions">
              <el-button link type="danger" :icon="Delete" @click="removePageBranch(branchKey)">删除该业务线</el-button>
            </div>
          </el-tab-pane>
        </el-tabs>
        <div class="add-branch-row">
          <el-button link type="primary" :icon="Plus" @click="addPageBranch">新增单页业务线</el-button>
        </div>

        <el-divider content-position="left">预算与监控</el-divider>
        <div class="budget-grid">
          <el-form-item label="默认时长（分钟）">
            <el-input-number
              :model-value="Math.round(profileForm.duration_seconds / 60)"
              :min="5"
              :max="120"
              @update:model-value="value => { profileForm.duration_seconds = Number(value) * 60 }"
            />
          </el-form-item>
          <el-form-item label="状态上限"><el-input-number v-model="profileForm.max_states" :min="1" /></el-form-item>
          <el-form-item label="设备动作上限"><el-input-number v-model="profileForm.max_device_actions" :min="1" /></el-form-item>
          <el-form-item label="深度上限"><el-input-number v-model="profileForm.max_depth" :min="1" /></el-form-item>
          <el-form-item label="每方向滚动次数"><el-input-number v-model="profileForm.max_scrolls_per_direction" :min="0" :max="20" /></el-form-item>
          <el-form-item label="全局覆盖滚动上限"><el-input-number v-model="profileForm.max_coverage_scroll_actions" :min="0" :max="1000" /></el-form-item>
          <el-form-item label="同类页面样本上限"><el-input-number v-model="profileForm.max_variants_per_cluster" :min="1" /></el-form-item>
          <el-form-item label="连续无新覆盖上限"><el-input-number v-model="profileForm.no_new_coverage_limit" :min="1" /></el-form-item>
          <el-form-item label="采集快照上限"><el-input-number v-model="profileForm.max_observations" :min="1" /></el-form-item>
          <el-form-item label="资产上限（MiB）"><el-input-number v-model="profileForm.max_artifact_mib" :min="64" /></el-form-item>
          <el-form-item label="稳定等待（秒）"><el-input-number v-model="profileForm.stable_wait_seconds" :min="1" :max="30" /></el-form-item>
        </div>
        <el-space wrap>
          <el-checkbox v-model="profileForm.enable_performance_monitor">CPU / 内存</el-checkbox>
          <el-checkbox v-model="profileForm.capture_log">Logcat</el-checkbox>
          <el-checkbox v-model="profileForm.enable_local_replay">30 秒前置 + 5 秒后置视频</el-checkbox>
          <el-checkbox v-model="profileForm.enable_jank_frame_monitor">卡顿帧监控</el-checkbox>
          <el-checkbox v-model="profileForm.enable_perfetto_trace" :disabled="!profileForm.enable_jank_frame_monitor">性能追踪（Perfetto）</el-checkbox>
        </el-space>

        <el-collapse class="advanced">
          <el-collapse-item title="高级规则（JSON 数组）" name="advanced">
            <el-alert title="输入规则只记录规则名称、变量和长度；密码请使用已标记为敏感的环境变量。" type="info" :closable="false" />
            <div class="json-grid">
              <el-form-item label="输入规则"><el-input v-model="profileForm.input_rules_text" type="textarea" :rows="7" /></el-form-item>
              <el-form-item label="自定义安全规则"><el-input v-model="profileForm.safety_rules_text" type="textarea" :rows="7" /></el-form-item>
              <el-form-item label="脱敏规则"><el-input v-model="profileForm.sanitizer_rules_text" type="textarea" :rows="7" /></el-form-item>
              <el-form-item label="动态文案正则"><el-input v-model="profileForm.dynamic_text_patterns_text" type="textarea" :rows="7" /></el-form-item>
            </div>
          </el-collapse-item>
        </el-collapse>
      </el-form>
      </div>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="loading.save" @click="saveProfile">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.inspection-page { height: 100%; overflow: auto; background: #f2f3f5; }
.content { min-height: 100%; padding: 12px; display: flex; flex-direction: column; gap: 12px; box-sizing: border-box; }
.header, .actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.title { font-size: 17px; font-weight: 700; color: #303133; }
.title.small { font-size: 15px; }
.subtitle { margin-top: 3px; font-size: 12px; color: #909399; }
.strong { font-weight: 600; color: #303133; }
.run-metric { min-width: 0; display: flex; flex-direction: column; gap: 2px; line-height: 1.3; }
.run-metric strong { color: #303133; font-size: 13px; }
.run-metric span { overflow: hidden; color: #909399; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.run-grid, .run-secondary-grid, .dialog-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 16px; }
.run-grid { grid-template-columns: minmax(190px, 1.1fr) minmax(210px, 1.15fr) minmax(150px, .8fr) minmax(250px, 1.35fr) auto; align-items: end; }
.run-grid :deep(.el-select), .run-secondary-grid :deep(.el-select), .dialog-grid :deep(.el-select) { width: 100%; }
.launch-form :deep(.el-form-item) { margin-bottom: 12px; }
.start-run-control { padding-bottom: 12px; }
.start-run-control :deep(.el-button) { min-width: 120px; height: 34px; }
.duration-control { width: 100%; display: flex; flex-direction: column; align-items: flex-start; gap: 6px; }
.duration-control :deep(.el-segmented) { width: 100%; }
.duration-preset-select { display: none; width: 100%; }
.duration-custom { display: flex; align-items: center; gap: 8px; color: #606266; }
.duration-custom :deep(.el-input-number) { width: 120px; }
.duration-allocation { font-size: 12px; color: #909399; }
.launch-more { border-top: 1px solid #ebeef5; border-bottom: 0; }
.launch-more :deep(.el-collapse-item__header) { min-height: 42px; height: auto; border-bottom: 0; }
.launch-more :deep(.el-collapse-item__wrap) { border-bottom: 0; }
.launch-more-title { min-width: 0; display: flex; align-items: baseline; gap: 10px; }
.launch-more-title strong { color: #303133; font-size: 13px; }
.launch-more-title span { overflow: hidden; color: #909399; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.safety-note { margin-bottom: 4px; padding: 8px 10px; color: #8a5a00; font-size: 12px; line-height: 1.45; border-left: 3px solid #e6a23c; background: #fdf6ec; }
.workspace-sections { display: flex; flex-direction: column; gap: 12px; }
.recent-panel { min-height: 238px; }
.recent-panel :deep(.el-card__body) { padding-top: 8px; }
.recent-runs-table :deep(.el-table__row) { cursor: pointer; }
.run-status-cell { display: flex; align-items: flex-start; flex-direction: column; gap: 2px; }
.profile-manager { padding: 0 16px; border: 1px solid #dcdfe6; background: #fff; }
.profile-manager :deep(.el-collapse-item__header) { min-height: 54px; height: auto; border-bottom: 0; }
.profile-manager :deep(.el-collapse-item__wrap) { border-bottom: 0; }
.profile-manager-title { min-width: 0; width: 100%; padding-right: 12px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.profile-manager-title > div { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.profile-manager-title strong { color: #303133; font-size: 14px; }
.profile-manager-title span { color: #909399; font-size: 12px; }
.budget-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0 16px; }
.scope-note { margin: 4px 0 8px; padding: 8px 10px; color: #606266; font-size: 12px; line-height: 1.45; border-left: 3px solid #409eff; background: #ecf5ff; }
.branch-actions { display: flex; justify-content: flex-end; }
.add-branch-row { margin: 4px 0 8px; }
.advanced { margin-top: 18px; }
.json-grid { margin-top: 12px; display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px; }
@media (max-width: 1200px) {
  .run-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .start-run-control { grid-column: 1 / -1; display: flex; justify-content: flex-end; }
}
@media (max-width: 760px) {
  .content { padding: 8px; }
  .header, .actions { flex-direction: column; align-items: stretch; }
  .actions :deep(.el-button) { width: 100%; margin-left: 0; }
  .run-grid, .run-secondary-grid, .dialog-grid, .budget-grid, .json-grid { grid-template-columns: minmax(0, 1fr); }
  .start-run-control { grid-column: auto; }
  .start-run-control :deep(.el-button) { width: 100%; }
  .duration-presets { display: none; }
  .duration-preset-select { display: block; }
}

.profile-dialog-scroll { width: 100%; min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
:global(.inspection-profile-dialog) { max-width: calc(100vw - 24px); max-height: calc(100dvh - 24px); margin: 0; display: flex; flex-direction: column; }
:global(.inspection-profile-dialog .el-dialog__header), :global(.inspection-profile-dialog .el-dialog__footer) { flex-shrink: 0; }
:global(.inspection-profile-dialog .el-dialog__body) { min-height: 0; display: flex; overflow: hidden; }
:global(.inspection-profile-dialog .el-dialog__footer) { position: sticky; z-index: 1; bottom: 0; border-top: 1px solid #ebeef5; background: #fff; }

@media (max-height: 620px) {
  .content { padding-top: 8px; gap: 8px; }
  :global(.inspection-profile-dialog) { max-height: calc(100dvh - 12px); }
}
</style>
