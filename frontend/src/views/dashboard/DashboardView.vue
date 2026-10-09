<script setup>
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import dayjs from 'dayjs'
import api from '@/api'
import { runStatusTagType as statusTagType, runStatusLabel as statusLabel } from '@/utils/statusMeta'
import { useClientMode } from '@/composables/useClientMode'
import VChart from 'vue-echarts'
import { chartTheme, chartColors } from '@/utils/chartTheme'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, PieChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
  TitleComponent,
} from 'echarts/components'

use([
  CanvasRenderer,
  LineChart,
  PieChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  TitleComponent,
])

const router = useRouter()
const { isMobileMode } = useClientMode()

const filters = reactive({
  range: '7d',
  platform: 'all',
})

const autoRefresh = ref(true)
const loading = ref(true)
const loaded = ref(false)
const errorMessage = ref('')

const emptyOverview = () => ({
  range: '7d',
  platform: 'all',
  generated_at: null,
  kpis: {
    total_executions: 0,
    pass_rate: 0,
    failed_scenarios: 0,
    avg_duration: 0,
    running_executions: 0,
    idle_devices: 0,
    active_tasks: 0,
  },
  trend: [],
  status_distribution: [],
  top_failed_scenarios: [],
  alerts: [],
  recent_executions: [],
  upcoming_tasks: [],
  api_automation: null,
})

const overview = ref(emptyOverview())
let pollTimer = null
let inflight = false
let liveBindingsActive = false

const rangeLabel = computed(() => {
  if (filters.range === '24h') return '近24小时'
  if (filters.range === '30d') return '近30天'
  return '近7天'
})

const kpiCards = computed(() => {
  const k = overview.value.kpis || {}
  const metric = (value) => loaded.value ? value : '—'
  return [
    { key: 'running', title: '正在运行', value: metric(k.running_executions || 0), route: '/execution/reports' },
    { key: 'failed', title: `${rangeLabel.value}失败场景`, value: metric(k.failed_scenarios || 0), route: '/ui/scenarios' },
    { key: 'idle', title: '可用设备', value: metric(k.idle_devices || 0), route: '/assets/devices' },
    { key: 'pass', title: `${rangeLabel.value}通过率`, value: loaded.value && k.total_executions > 0 ? `${Number(k.pass_rate || 0).toFixed(1)}%` : '—', route: '/execution/reports' },
  ]
})
const mobileKpiCards = kpiCards
const hasExecutions = computed(() => loaded.value && overview.value.kpis.total_executions > 0)
const runningExecutions = computed(() => (overview.value.recent_executions || []).filter(item => ['RUNNING', 'PENDING', 'QUEUED'].includes(item.status)))

const recentProblemExecutions = computed(() => {
  const problemStatuses = new Set(['FAIL', 'ERROR', 'WARNING'])
  return (overview.value.recent_executions || [])
    .filter(item => problemStatuses.has(String(item.status || '').toUpperCase()))
    .slice(0, 5)
})

const trendLine = (name, data, color) => ({ name, type: 'line', smooth: true, data, itemStyle: { color }, lineStyle: { width: 2, color } })
const buildTrendOption = (trend, { includeWarning = true } = {}) => ({
  tooltip: { trigger: 'axis' },
  legend: { top: 4 },
  grid: { left: 36, right: 20, top: 34, bottom: 22, containLabel: true },
  xAxis: { type: 'category', boundaryGap: false, data: trend.map(item => item.date) },
  yAxis: { type: 'value' },
  series: [
    trendLine('总执行', trend.map(item => item.total), chartColors.primary),
    trendLine('通过', trend.map(item => item.pass_count), chartColors.success),
    trendLine('失败', trend.map(item => item.fail_count), chartColors.danger),
    ...(includeWarning ? [trendLine('告警', trend.map(item => item.warning_count), chartColors.warning)] : []),
  ],
})
const trendOption = computed(() => buildTrendOption(overview.value.trend || []))

// Interface automation is a separate record family with its own block; its
// runs never carry a WARNING status, so that series is omitted.
const apiBlock = computed(() => overview.value.api_automation || null)
const hasApiRuns = computed(() => loaded.value && (apiBlock.value?.total_runs || 0) > 0)
const apiTrendOption = computed(() => buildTrendOption(apiBlock.value?.trend || [], { includeWarning: false }))
const apiStats = computed(() => {
  const block = apiBlock.value || {}
  const metric = (value) => loaded.value ? value : '—'
  return [
    { key: 'runs', title: `${rangeLabel.value}执行`, value: metric(block.total_runs || 0) },
    { key: 'pass', title: '通过率', value: loaded.value && block.completed_runs > 0 ? `${Number(block.pass_rate || 0).toFixed(1)}%` : '—' },
    { key: 'failed', title: '失败', value: metric(block.failed_runs || 0) },
    { key: 'running', title: '运行中', value: metric(block.running_runs || 0) },
    { key: 'duration', title: '平均耗时', value: loaded.value ? formatDuration(block.avg_duration) : '—' },
  ]
})
const handleOpenApiRun = (row) => {
  if (row?.id) router.push(`/execution/reports/api/${row.id}`)
}

const statusPieOption = computed(() => {
  const labelMap = {
    PASS: '通过',
    WARNING: '告警',
    FAIL: '失败',
    ERROR: '错误',
    ABORTED: '已终止',
    RUNNING: '运行中',
  }
  const colorMap = {
    PASS: chartColors.success,
    WARNING: chartColors.warning,
    FAIL: chartColors.danger,
    ERROR: chartColors.danger,
    ABORTED: chartColors.muted,
    RUNNING: chartColors.primary,
  }
  const rows = (overview.value.status_distribution || [])
    .filter(item => item.count > 0)
    .map(item => ({
      name: labelMap[item.status] || item.status,
      value: item.count,
      itemStyle: { color: colorMap[item.status] || chartColors.muted },
    }))

  return {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0 },
    series: [
      {
        type: 'pie',
        radius: ['42%', '70%'],
        center: ['50%', '42%'],
        data: rows,
        label: { formatter: '{b}: {c}' },
      },
    ],
  }
})

const stopPolling = () => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

const startPolling = () => {
  stopPolling()
  if (!autoRefresh.value || document.hidden) return
  pollTimer = setInterval(() => {
    fetchOverview({ silent: true })
  }, 15000)
}

const fetchOverview = async ({ silent = false } = {}) => {
  if (inflight) return
  inflight = true
  if (!silent) loading.value = true
  try {
    const { data } = await api.getDashboardOverview({
      range: filters.range,
      platform: filters.platform,
      limit_recent: 10,
      limit_tasks: 8,
    })
    overview.value = { ...emptyOverview(), ...data }
    loaded.value = true
    errorMessage.value = ''
  } catch (err) {
    const msg = err?.response?.data?.detail || err?.message || '加载运行大盘失败'
    errorMessage.value = msg
    if (!silent) ElMessage.error(msg)
  } finally {
    inflight = false
    loading.value = false
  }
}

const handleVisibilityChange = () => {
  if (document.hidden) stopPolling()
  else startPolling()
}

const activateLiveBindings = () => {
  if (liveBindingsActive) return
  liveBindingsActive = true
  startPolling()
  document.addEventListener('visibilitychange', handleVisibilityChange)
}

const deactivateLiveBindings = () => {
  if (!liveBindingsActive) return
  liveBindingsActive = false
  stopPolling()
  document.removeEventListener('visibilitychange', handleVisibilityChange)
}

const handleKpiClick = (item) => {
  if (item?.route) router.push(item.route)
}

const handleOpenRecent = (row) => {
  if (!row?.id) return
  router.push(`/execution/reports/${row.id}`)
}

const formatDateTime = (value) => {
  if (!value) return '-'
  return dayjs(value).format('MM-DD HH:mm:ss')
}

const formatDuration = (seconds) => {
  const duration = Number(seconds || 0)
  if (!duration) return '-'
  if (duration < 60) return `${Math.round(duration)}s`
  const m = Math.floor(duration / 60)
  const s = Math.round(duration % 60)
  return `${m}m ${s}s`
}

const alertType = (level) => {
  if (level === 'danger') return 'error'
  if (level === 'warning') return 'warning'
  return 'info'
}

watch(
  () => [filters.range, filters.platform],
  async () => {
    await fetchOverview()
    startPolling()
  }
)

watch(
  () => autoRefresh.value,
  () => startPolling()
)

onMounted(async () => {
  await fetchOverview()
  activateLiveBindings()
})

onActivated(() => {
  activateLiveBindings()
})

onDeactivated(() => {
  deactivateLiveBindings()
})

onUnmounted(() => {
  deactivateLiveBindings()
})
</script>

<template>
  <div class="dashboard-page">
    <div v-if="isMobileMode" class="mobile-dashboard" v-loading="loading">
      <el-alert
        v-if="errorMessage"
        type="error"
        title="运行大盘加载失败"
        show-icon
        :closable="false"
        class="error-alert"
      >
        <template #default>
          <div class="mobile-error-content">
            <span>{{ errorMessage }}</span>
            <el-button link type="primary" :loading="loading" @click="fetchOverview()">重试</el-button>
          </div>
        </template>
      </el-alert>

      <div class="mobile-kpi-grid">
        <button
          v-for="item in mobileKpiCards"
          :key="item.key"
          class="mobile-kpi-card"
          type="button"
          @click="handleKpiClick(item)"
        >
          <span>{{ item.title }}</span>
          <strong>{{ item.value }}</strong>
        </button>
      </div>

      <section class="mobile-panel">
        <div class="mobile-panel-header">
          <h3>最近异常</h3>
          <el-button link type="primary" @click="router.push('/execution/reports')">全部报告</el-button>
        </div>
        <div v-if="recentProblemExecutions.length > 0" class="mobile-execution-list">
          <button type="button"
            v-for="item in recentProblemExecutions"
            :key="item.id"
            class="mobile-execution-item"
            @click="handleOpenRecent(item)"
          >
            <div class="mobile-execution-main">
              <strong>{{ item.scenario_name || '未命名场景' }}</strong>
              <span>{{ formatDateTime(item.start_time) }} · {{ item.executor_name || 'System' }}</span>
            </div>
            <el-tag size="small" :type="statusTagType(item.status)">{{ statusLabel(item.status) }}</el-tag>
          </button>
        </div>
        <div v-else class="ad-empty-state mobile-inline-empty">
          <h3>{{ loaded ? '暂无异常执行' : '正在获取运行状态…' }}</h3>
          <p>{{ loaded ? '当前时间范围内没有失败、告警或排队记录。' : '数据加载完成后会显示需要优先处理的执行。' }}</p>
          <el-button v-if="errorMessage" link type="primary" @click="fetchOverview()">重新加载</el-button>
        </div>
      </section>

      <section class="mobile-panel">
        <div class="mobile-panel-header">
          <h3>最近执行</h3>
          <el-button link type="primary" @click="router.push('/execution/reports')">查看</el-button>
        </div>
        <div v-if="overview.recent_executions?.length" class="mobile-execution-list">
          <button type="button"
            v-for="item in (overview.recent_executions || []).slice(0, 5)"
            :key="item.id"
            class="mobile-execution-item"
            @click="handleOpenRecent(item)"
          >
            <div class="mobile-execution-main">
              <strong>{{ item.scenario_name || '未命名场景' }}</strong>
              <span>{{ formatDateTime(item.start_time) }} · {{ formatDuration(item.duration) }}</span>
            </div>
            <el-tag size="small" :type="statusTagType(item.status)">{{ statusLabel(item.status) }}</el-tag>
          </button>
        </div>
        <div v-else class="ad-empty-state mobile-inline-empty">
          <h3>{{ loaded ? '暂无执行记录' : '正在获取执行记录…' }}</h3>
          <p>{{ loaded ? '前往用例或场景列表开始一次执行。' : '请稍候。' }}</p>
          <el-button v-if="loaded" link type="primary" @click="router.push('/ui/cases')">前往用例库</el-button>
          <el-button v-else-if="errorMessage" link type="primary" @click="fetchOverview()">重新加载</el-button>
        </div>
      </section>
    </div>

    <div v-else class="dashboard-scroll" v-loading="loading">
      <header class="ad-page-header dashboard-header">
        <div><h1>运行大盘</h1><p class="dashboard-subtitle">关注异常与当前执行，快速回到工作现场。</p></div>
        <div class="dashboard-filters">
          <el-select v-model="filters.range" aria-label="统计时间范围" style="width: 116px">
            <el-option label="近24小时" value="24h" /><el-option label="近7天" value="7d" /><el-option label="近30天" value="30d" />
          </el-select>
          <el-select v-model="filters.platform" aria-label="平台筛选" style="width: 116px">
            <el-option label="全部平台" value="all" /><el-option label="Android" value="android" /><el-option label="iOS" value="ios" />
          </el-select>
          <el-checkbox v-model="autoRefresh">自动刷新</el-checkbox>
          <el-button :loading="loading" @click="fetchOverview()">刷新</el-button>
        </div>
      </header>
      <el-alert v-if="errorMessage" type="error" :title="errorMessage" :description="loaded ? '当前显示上次成功加载的数据，可点击刷新重试。' : '数据暂时无法加载，请点击刷新重试。'" show-icon :closable="false" />
      <div class="kpi-grid">
        <button v-for="item in kpiCards" :key="item.key" class="kpi-card" type="button" @click="handleKpiClick(item)">
          <span class="kpi-title">{{ item.title }}</span><strong class="kpi-value">{{ item.value }}</strong>
        </button>
      </div>
      <div v-if="loaded" class="dashboard-summary">{{ rangeLabel }}共 {{ overview.kpis.total_executions }} 次执行 · {{ overview.kpis.active_tasks }} 个启用任务 · 平均耗时 {{ formatDuration(overview.kpis.avg_duration) }}</div>
      <section class="block-grid activity-grid">
        <el-card shadow="never" class="panel-card">
          <template #header><div class="panel-header"><span>最近执行</span><el-button link type="primary" @click="router.push('/execution/reports')">全部报告</el-button></div></template>
          <el-table v-if="overview.recent_executions.length" :data="overview.recent_executions" size="small">
            <el-table-column label="名称" min-width="170" show-overflow-tooltip><template #default="{ row }"><button class="ad-name-button" @click="handleOpenRecent(row)">{{ row.scenario_name || '未命名场景' }}</button></template></el-table-column>
            <el-table-column label="状态" width="92"><template #default="{ row }"><el-tag size="small" :type="statusTagType(row.status)">{{ statusLabel(row.status) }}</el-tag></template></el-table-column>
            <el-table-column label="开始时间" width="116"><template #default="{ row }">{{ formatDateTime(row.start_time) }}</template></el-table-column>
            <el-table-column label="耗时" width="80"><template #default="{ row }">{{ formatDuration(row.duration) }}</template></el-table-column>
          </el-table>
          <div v-else class="ad-empty-state"><h3>{{ loaded ? '开始第一次执行' : '等待执行数据' }}</h3><p>选择用例或场景运行后，在这里查看执行结果。</p><el-button @click="router.push('/ui/cases')">前往用例库</el-button></div>
        </el-card>
        <el-card shadow="never" class="panel-card">
          <template #header><div class="panel-header">需要关注</div></template>
          <div class="alerts-list">
            <el-alert v-for="(item, idx) in overview.alerts" :key="idx" :type="alertType(item.level)" :title="item.title" :description="item.message" show-icon :closable="false" />
            <button v-for="item in runningExecutions" :key="item.id" class="activity-link" @click="handleOpenRecent(item)"><span>{{ item.scenario_name || '未命名场景' }}</span><el-tag :type="statusTagType(item.status)" size="small">{{ statusLabel(item.status) }}</el-tag></button>
            <button v-for="item in recentProblemExecutions" :key="item.id" class="activity-link" @click="handleOpenRecent(item)"><span>{{ item.scenario_name || '未命名场景' }}</span><el-tag :type="statusTagType(item.status)" size="small">{{ statusLabel(item.status) }}</el-tag></button>
            <p v-if="!overview.alerts.length && !recentProblemExecutions.length && !runningExecutions.length" class="quiet-state">{{ loaded ? '当前没有待处理异常或运行中的任务。' : '正在获取运行状态…' }}</p>
          </div>
        </el-card>
      </section>
      <section class="block-grid two-col">
        <el-card shadow="never" class="panel-card" :class="{ 'span-all': !hasApiRuns }">
          <template #header><div class="panel-header"><span>接口自动化</span><el-button link type="primary" @click="router.push('/execution/reports?tab=api')">全部接口报告</el-button></div></template>
          <div class="api-stats"><div v-for="item in apiStats" :key="item.key" class="api-stat"><span class="kpi-title">{{ item.title }}</span><strong class="api-stat-value">{{ item.value }}</strong></div></div>
          <el-table v-if="apiBlock?.recent_runs?.length" :data="apiBlock.recent_runs" size="small">
            <el-table-column label="场景" min-width="150" show-overflow-tooltip><template #default="{ row }"><button class="ad-name-button" @click="handleOpenApiRun(row)">{{ row.scenario_name || '未命名场景' }}</button></template></el-table-column>
            <el-table-column prop="env_name" label="环境" min-width="90" show-overflow-tooltip />
            <el-table-column label="状态" width="88"><template #default="{ row }"><el-tag size="small" :type="statusTagType(row.status)">{{ statusLabel(row.status) }}</el-tag></template></el-table-column>
            <el-table-column label="开始时间" width="116"><template #default="{ row }">{{ formatDateTime(row.start_time) }}</template></el-table-column>
            <el-table-column label="耗时" width="72"><template #default="{ row }">{{ formatDuration(row.duration) }}</template></el-table-column>
          </el-table>
          <div v-else class="ad-empty-state"><h3>{{ loaded ? '还没有接口运行' : '等待接口数据' }}</h3><p>编排接口场景并运行后，结果会汇总到这里。</p><el-button @click="router.push('/api-testing/scenarios')">前往自动化场景</el-button></div>
        </el-card>
        <el-card v-if="hasApiRuns" shadow="never" class="panel-card"><template #header><div class="panel-header">接口执行趋势</div></template><v-chart class="chart-box" :theme="chartTheme" :option="apiTrendOption" autoresize /></el-card>
      </section>
      <section v-if="hasExecutions" class="block-grid two-col">
        <el-card shadow="never" class="panel-card"><template #header><div class="panel-header">执行趋势</div></template><v-chart class="chart-box" :theme="chartTheme" :option="trendOption" autoresize /></el-card>
        <el-card shadow="never" class="panel-card"><template #header><div class="panel-header">状态分布</div></template><v-chart class="chart-box" :theme="chartTheme" :option="statusPieOption" autoresize /></el-card>
      </section>
      <section class="block-grid two-col">
        <el-card shadow="never" class="panel-card">
          <template #header><div class="panel-header">高失败场景</div></template>
          <el-table :data="overview.top_failed_scenarios" size="small" :empty-text="loaded ? '所选时间范围内暂无失败场景' : '等待数据'">
            <el-table-column prop="name" label="场景" min-width="160" show-overflow-tooltip /><el-table-column prop="fail_count" label="失败次数" width="90" />
            <el-table-column label="失败率" width="80"><template #default="{ row }">{{ Number(row.fail_rate || 0).toFixed(1) }}%</template></el-table-column>
          </el-table>
        </el-card>
        <el-card shadow="never" class="panel-card">
          <template #header><div class="panel-header"><span>即将执行任务</span><el-button link @click="router.push('/execution/tasks')">管理任务</el-button></div></template>
          <el-table :data="overview.upcoming_tasks" size="small" :empty-text="loaded ? '暂无计划中的任务' : '等待数据'">
            <el-table-column prop="name" label="任务" min-width="140" show-overflow-tooltip /><el-table-column prop="scenario_name" label="场景" min-width="110" show-overflow-tooltip />
            <el-table-column label="下次执行" width="116"><template #default="{ row }">{{ formatDateTime(row.next_run_time) }}</template></el-table-column>
          </el-table>
        </el-card>
      </section>
    </div>
  </div>
</template>

<style scoped>
.dashboard-page { height: 100%; min-height: 0; display: flex; flex-direction: column; background: var(--ad-bg); }
.dashboard-scroll { flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; padding: 16px; display: flex; flex-direction: column; gap: 16px; }
.dashboard-header { margin-bottom: 0; }
.dashboard-subtitle { margin: 4px 0 0; color: var(--ad-muted); font-size: 12px; }
.dashboard-filters { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.kpi-card { border: 1px solid var(--ad-border); background: var(--ad-surface); border-radius: 8px; padding: 16px; text-align: left; }
.kpi-card:hover { border-color: var(--ad-primary); }
.kpi-title { display: block; font-size: 12px; color: var(--ad-muted); }
.kpi-value { display: block; margin-top: 8px; font-size: 26px; line-height: 1.2; color: var(--ad-text); font-weight: 600; font-variant-numeric: tabular-nums; }
.dashboard-summary { margin-top: -8px; color: var(--ad-muted); font-size: 12px; }
.block-grid { display: grid; gap: 16px; }
.two-col { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.span-all { grid-column: 1 / -1; }
.api-stats { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-bottom: 12px; }
.api-stat { padding: 10px 12px; border: 1px solid var(--ad-border); border-radius: 8px; background: var(--ad-bg); min-width: 0; }
.api-stat-value { display: block; margin-top: 6px; font-size: 20px; line-height: 1.2; font-weight: 600; color: var(--ad-text); font-variant-numeric: tabular-nums; }
.activity-grid { grid-template-columns: minmax(0, 1.65fr) minmax(280px, 1fr); }
.panel-card { min-width: 0; border-radius: var(--ad-panel-radius); }
.panel-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 13px; font-weight: 600; color: var(--ad-text); min-height: 24px; }
.chart-box { height: 240px; width: 100%; }
.alerts-list { display: flex; flex-direction: column; gap: 8px; }
.activity-link { display: flex; align-items: center; justify-content: space-between; gap: 8px; width: 100%; border: 0; border-bottom: 1px solid var(--ad-border); padding: 8px 0; background: transparent; font: inherit; text-align: left; color: var(--ad-text); }
.activity-link > span { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.quiet-state { margin: 8px 0; color: var(--ad-muted); }
.mobile-dashboard { flex: 1; min-height: 0; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 12px; }
.mobile-error-content { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.mobile-inline-empty { padding: 20px 8px; }
.mobile-kpi-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.mobile-kpi-card { border: 1px solid var(--ad-border); border-radius: 8px; background: var(--ad-surface); padding: 14px; text-align: left; display: flex; flex-direction: column; gap: 8px; }
.mobile-kpi-card span { font-size: 14px; color: var(--ad-muted); }
.mobile-kpi-card strong { font-size: 24px; color: var(--ad-text); line-height: 1.2; }
.mobile-panel { border: 1px solid var(--ad-border); border-radius: 8px; background: var(--ad-surface); padding: 12px; }
.mobile-panel-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.mobile-panel-header h3 { margin: 0; font-size: 16px; }
.mobile-execution-list { display: flex; flex-direction: column; gap: 8px; }
.mobile-execution-item { width: 100%; background: transparent; text-align: left; border: 1px solid var(--ad-border); border-radius: 6px; padding: 10px; display: flex; align-items: center; justify-content: space-between; gap: 8px; cursor: pointer; }
.mobile-execution-main { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.mobile-execution-main strong { font-size: 14px; color: var(--ad-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mobile-execution-main span { font-size: 14px; color: var(--ad-muted); }
@media (max-width: 1100px) { .activity-grid, .two-col { grid-template-columns: 1fr; } .api-stats { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 720px) { .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
