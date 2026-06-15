<script setup>
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import dayjs from 'dayjs'
import api from '@/api'
import { useClientMode } from '@/composables/useClientMode'
import VChart from 'vue-echarts'
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
const loading = ref(false)
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
  return [
    { key: 'pass_rate', title: `${rangeLabel.value}通过率`, value: `${Number(k.pass_rate || 0).toFixed(1)}%`, route: '/execution/reports' },
    { key: 'failed_scenarios', title: `${rangeLabel.value}失败场景数`, value: k.failed_scenarios || 0, route: '/ui/scenarios' },
    { key: 'idle_devices', title: '当前空闲设备', value: k.idle_devices || 0, route: '/assets/devices' },
    { key: 'total_executions', title: `${rangeLabel.value}执行总量`, value: k.total_executions || 0, route: '/execution/reports' },
    { key: 'running_executions', title: '运行中执行数', value: k.running_executions || 0, route: '/execution/reports' },
    { key: 'active_tasks', title: '启用任务数', value: k.active_tasks || 0, route: '/execution/tasks' },
  ]
})

const mobileKpiCards = computed(() => {
  const k = overview.value.kpis || {}
  return [
    { key: 'running', title: '运行中', value: k.running_executions || 0, route: '/execution/reports' },
    { key: 'idle', title: '空闲设备', value: k.idle_devices || 0, route: '/assets/devices' },
    { key: 'pass', title: `${rangeLabel.value}通过率`, value: `${Number(k.pass_rate || 0).toFixed(1)}%`, route: '/execution/reports' },
    { key: 'failed', title: '失败场景', value: k.failed_scenarios || 0, route: '/ui/scenarios' },
  ]
})

const recentProblemExecutions = computed(() => {
  const problemStatuses = new Set(['FAIL', 'ERROR', 'WARNING'])
  return (overview.value.recent_executions || [])
    .filter(item => problemStatuses.has(String(item.status || '').toUpperCase()))
    .slice(0, 5)
})

const trendOption = computed(() => {
  const trend = overview.value.trend || []
  return {
    tooltip: { trigger: 'axis' },
    legend: { top: 4 },
    grid: { left: 36, right: 20, top: 34, bottom: 22, containLabel: true },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: trend.map(item => item.date),
    },
    yAxis: { type: 'value' },
    series: [
      {
        name: '总执行',
        type: 'line',
        smooth: true,
        data: trend.map(item => item.total),
        itemStyle: { color: '#409EFF' },
        lineStyle: { width: 2, color: '#409EFF' },
      },
      {
        name: '通过',
        type: 'line',
        smooth: true,
        data: trend.map(item => item.pass_count),
        itemStyle: { color: '#67C23A' },
        lineStyle: { width: 2, color: '#67C23A' },
      },
      {
        name: '失败',
        type: 'line',
        smooth: true,
        data: trend.map(item => item.fail_count),
        itemStyle: { color: '#F56C6C' },
        lineStyle: { width: 2, color: '#F56C6C' },
      },
      {
        name: '告警',
        type: 'line',
        smooth: true,
        data: trend.map(item => item.warning_count),
        itemStyle: { color: '#E6A23C' },
        lineStyle: { width: 2, color: '#E6A23C' },
      },
    ],
  }
})

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
    PASS: '#67C23A',
    WARNING: '#E6A23C',
    FAIL: '#F56C6C',
    ERROR: '#D03050',
    ABORTED: '#909399',
    RUNNING: '#409EFF',
  }
  const rows = (overview.value.status_distribution || [])
    .filter(item => item.count > 0)
    .map(item => ({
      name: labelMap[item.status] || item.status,
      value: item.count,
      itemStyle: { color: colorMap[item.status] || '#909399' },
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

const statusTagType = (status) => {
  const normalized = (status || '').toUpperCase()
  if (normalized === 'PASS') return 'success'
  if (normalized === 'WARNING') return 'warning'
  if (normalized === 'FAIL' || normalized === 'ERROR') return 'danger'
  if (normalized === 'ABORTED') return 'info'
  if (normalized === 'RUNNING') return 'info'
  return 'info'
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
        :title="errorMessage"
        show-icon
        :closable="false"
        class="error-alert"
      />

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
          <article
            v-for="item in recentProblemExecutions"
            :key="item.id"
            class="mobile-execution-item"
            @click="handleOpenRecent(item)"
          >
            <div class="mobile-execution-main">
              <strong>{{ item.scenario_name || '未命名场景' }}</strong>
              <span>{{ formatDateTime(item.start_time) }} · {{ item.executor_name || 'System' }}</span>
            </div>
            <el-tag size="small" :type="statusTagType(item.status)">{{ item.status }}</el-tag>
          </article>
        </div>
        <el-empty v-else description="暂无异常执行" :image-size="80" />
      </section>

      <section class="mobile-panel">
        <div class="mobile-panel-header">
          <h3>最近执行</h3>
          <el-button link type="primary" @click="router.push('/execution/reports')">查看</el-button>
        </div>
        <div class="mobile-execution-list">
          <article
            v-for="item in (overview.recent_executions || []).slice(0, 5)"
            :key="item.id"
            class="mobile-execution-item"
            @click="handleOpenRecent(item)"
          >
            <div class="mobile-execution-main">
              <strong>{{ item.scenario_name || '未命名场景' }}</strong>
              <span>{{ formatDateTime(item.start_time) }} · {{ formatDuration(item.duration) }}</span>
            </div>
            <el-tag size="small" :type="statusTagType(item.status)">{{ item.status }}</el-tag>
          </article>
        </div>
      </section>
    </div>

    <div v-else class="dashboard-scroll" v-loading="loading">
      <el-alert
        v-if="errorMessage"
        type="error"
        :title="errorMessage"
        show-icon
        :closable="false"
        class="error-alert"
      />

      <div class="kpi-grid">
        <el-card
          v-for="item in kpiCards"
          :key="item.key"
          shadow="hover"
          class="kpi-card"
          @click="handleKpiClick(item)"
        >
          <div class="kpi-title">{{ item.title }}</div>
          <div class="kpi-value">{{ item.value }}</div>
        </el-card>
      </div>

      <div class="block-grid two-col">
        <el-card shadow="never" class="panel-card">
          <template #header>
            <div class="panel-header">执行趋势</div>
          </template>
          <v-chart class="chart-box" :option="trendOption" autoresize />
        </el-card>

        <el-card shadow="never" class="panel-card">
          <template #header>
            <div class="panel-header">状态分布</div>
          </template>
          <v-chart class="chart-box" :option="statusPieOption" autoresize />
        </el-card>
      </div>

      <div class="block-grid two-col">
        <el-card shadow="never" class="panel-card">
          <template #header>
            <div class="panel-header">高失败场景 Top 5</div>
          </template>
          <el-table :data="overview.top_failed_scenarios" size="small" empty-text="暂无数据">
            <el-table-column prop="name" label="场景" min-width="160" show-overflow-tooltip />
            <el-table-column prop="fail_count" label="失败次数" width="110" />
            <el-table-column label="失败率" width="100">
              <template #default="{ row }">
                {{ Number(row.fail_rate || 0).toFixed(1) }}%
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card shadow="never" class="panel-card">
          <template #header>
            <div class="panel-header">异常告警</div>
          </template>
          <div v-if="!overview.alerts || overview.alerts.length === 0" class="empty-wrap">
            <el-empty description="暂无告警" :image-size="80" />
          </div>
          <div v-else class="alerts-list">
            <el-alert
              v-for="(item, idx) in overview.alerts"
              :key="`${item.type}-${idx}`"
              :type="alertType(item.level)"
              :title="item.title"
              :description="item.message"
              show-icon
              :closable="false"
            />
          </div>
        </el-card>
      </div>

      <div class="block-grid two-col">
        <el-card shadow="never" class="panel-card">
          <template #header>
            <div class="panel-header">最近执行</div>
          </template>
          <el-table
            :data="overview.recent_executions"
            size="small"
            empty-text="暂无执行记录"
            @row-click="handleOpenRecent"
            class="clickable-table"
          >
            <el-table-column prop="start_time" label="开始时间" width="130">
              <template #default="{ row }">{{ formatDateTime(row.start_time) }}</template>
            </el-table-column>
            <el-table-column prop="scenario_name" label="场景" min-width="170" show-overflow-tooltip />
            <el-table-column prop="platform" label="平台" width="86">
              <template #default="{ row }">{{ (row.platform || '-').toUpperCase() }}</template>
            </el-table-column>
            <el-table-column prop="status" label="状态" width="92">
              <template #default="{ row }">
                <el-tag size="small" :type="statusTagType(row.status)">{{ row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="duration" label="耗时" width="90">
              <template #default="{ row }">{{ formatDuration(row.duration) }}</template>
            </el-table-column>
            <el-table-column prop="executor_name" label="执行人" width="100" show-overflow-tooltip />
          </el-table>
        </el-card>

        <el-card shadow="never" class="panel-card">
          <template #header>
            <div class="panel-header">即将执行任务</div>
          </template>
          <el-table :data="overview.upcoming_tasks" size="small" empty-text="暂无任务">
            <el-table-column prop="name" label="任务" min-width="160" show-overflow-tooltip />
            <el-table-column prop="scenario_name" label="场景" min-width="120" show-overflow-tooltip />
            <el-table-column prop="next_run_time" label="下次执行" width="130">
              <template #default="{ row }">{{ formatDateTime(row.next_run_time) }}</template>
            </el-table-column>
            <el-table-column prop="formatted_schedule" label="调度策略" min-width="160" show-overflow-tooltip />
          </el-table>
        </el-card>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dashboard-page {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: #f2f3f5;
}

.dashboard-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 10px;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.error-alert {
  margin-top: 2px;
}

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 10px;
}

.kpi-card {
  cursor: pointer;
}

.kpi-title {
  font-size: 13px;
  color: #606266;
}

.kpi-value {
  margin-top: 8px;
  font-size: 26px;
  line-height: 1;
  color: #303133;
  font-weight: 600;
}

.block-grid {
  display: grid;
  gap: 10px;
}

.two-col {
  grid-template-columns: 1fr 1fr;
}

.panel-card {
  border-radius: 6px;
}

.panel-header {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

.chart-box {
  height: 320px;
  width: 100%;
}

.alerts-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.empty-wrap {
  min-height: 240px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.clickable-table :deep(.el-table__row) {
  cursor: pointer;
}

.mobile-dashboard {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: #f6f7f9;
}

.mobile-kpi-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.mobile-kpi-card {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  background: #ffffff;
  padding: 14px;
  text-align: left;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.mobile-kpi-card span {
  font-size: 12px;
  color: #606266;
}

.mobile-kpi-card strong {
  font-size: 24px;
  color: #303133;
  line-height: 1;
}

.mobile-panel {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  background: #ffffff;
  padding: 14px;
}

.mobile-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.mobile-panel-header h3 {
  margin: 0;
  font-size: 15px;
  color: #303133;
}

.mobile-execution-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.mobile-execution-item {
  border: 1px solid #f0f2f5;
  border-radius: 6px;
  padding: 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  cursor: pointer;
}

.mobile-execution-main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.mobile-execution-main strong {
  font-size: 14px;
  color: #303133;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mobile-execution-main span {
  font-size: 12px;
  color: #909399;
}

@media (max-width: 1400px) {
  .kpi-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 900px) {
  .two-col {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 720px) {
  .kpi-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .kpi-value {
    font-size: 22px;
  }

  .chart-box {
    height: 280px;
  }
}
</style>
