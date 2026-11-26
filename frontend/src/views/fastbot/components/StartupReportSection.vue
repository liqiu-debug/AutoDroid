<script setup>
import { computed } from 'vue'
import StatCard from './StatCard.vue'
import api from '@/api'
import { formatDiagnosisStatus, formatMs, formatReadyStatus, formatStartupMode, formatTime, formatTraceAnalysisStatus, getPrimaryTraceCause } from './reportFormatters'
import { runStatusTagType, runStatusLabel } from '@/utils/statusMeta'
import {
    startupPlatform, startupPlatformLabel, startupExecutorLabel, startupCounts,
    startupMetricLabel, startupMetricDescription, startupMetricStats, startupVerdicts,
    startupThresholdMetric, isStartupV2, isStartupActive,
    startupStateText, startupMetricSourceText, startupMetricMetadata, startupTraceEvidence,
} from '@/utils/startup'

const props = defineProps({
    task: { type: Object, default: () => ({}) },
    summary: { type: Object, default: () => ({}) },
    traceArtifacts: { type: Array, default: () => [] },
    deviceName: { type: String, default: '' },
    analyzedCount: { type: Number, default: 0 },
    batchLoading: { type: Boolean, default: false },
})
defineEmits(['open-trace-ai', 'generate-summaries'])
const startupConfig = computed(() => props.summary?.startup_config || {})
const startupRuns = computed(() => Array.isArray(props.summary?.startup_runs) ? props.summary.startup_runs : [])
const traces = computed(() => props.traceArtifacts.map(artifact => ({
    ...artifact, sourceArtifact: artifact, startupEvidence: startupTraceEvidence(artifact),
    startupOnly: artifact.analysis_level === 'startup_evidence' || artifact.analysis?.analysis_level === 'startup_evidence',
})))
const legacyAnalyzedCount = computed(() => traces.value.filter(artifact => !artifact.startupOnly && !artifact.startupEvidence && artifact.analysis_status === 'ANALYZED').length)
const slowEvents = computed(() => Array.isArray(props.summary?.slow_events) ? props.summary.slow_events : [])
const platform = computed(() => startupPlatform(props.summary.platform || startupConfig.value.platform || props.task.platform))
const v2 = computed(() => isStartupV2(props.summary))
const counts = computed(() => startupCounts(props.summary))
const verdicts = computed(() => startupVerdicts(props.summary))
const startupModesLabel = computed(() => (startupConfig.value.startup_modes || []).map(formatStartupMode).join(' / ') || '—')
const metricRows = computed(() => {
    const modes = startupConfig.value.startup_modes || Object.keys(props.summary.startup_aggregate || {})
    const metrics = [...(v2.value ? ['automation_ms'] : []), ...(platform.value === 'android' ? ['total_time_ms'] : []), 'ready_ms']
    return modes.flatMap(mode => metrics.map(metric => ({
        mode, metric, label: startupMetricLabel(metric, platform.value),
        ...(startupMetricStats(props.summary, mode, metric) || {}),
    })))
})
const sampleText = row => row.count === null || row.count === undefined ? '历史报告未记录' : `${row.count}${row.samples_sufficient === false || row.count < 20 ? ' · 样本不足' : ''}`
const isSampleInsufficient = row => row.count !== null && row.count !== undefined && (row.samples_sufficient === false || row.count < 20)
const formatOutcome = (value, yes = '成功', no = '失败') => value === true ? yes : value === false ? no : '未评估'
const outcomeClass = value => value === true ? 'tone-success' : value === false ? 'tone-danger' : 'tone-muted'
const launchOutcome = row => formatOutcome(v2.value ? row.launch_success : row.success)
const sampleOutcome = row => row.diagnostic ? '诊断复跑（不计入统计）' : !v2.value ? '历史口径未校验' : row.status === 'ABORTED' || row.completed === false ? '采样已中断（不计入统计）' : row.sample_valid === true ? '有效' : row.status === 'INVALID' ? '无效' : '无有效样本'
const shortSampleOutcome = row => row.diagnostic ? '诊断复跑' : !v2.value ? '未校验' : row.status === 'ABORTED' || row.completed === false ? '已中断' : row.sample_valid === true ? '有效' : row.status === 'INVALID' ? '无效' : '无效样本'
const modeMismatch = row => Boolean(row.actual_mode ? row.actual_mode !== (row.requested_mode || row.mode) : true)
const metricMetadata = computed(() => startupMetricMetadata(props.summary, platform.value))
const threshold = mode => startupConfig.value.thresholds?.[`${mode}_ms`] ?? startupConfig.value.perfetto_slow_trace?.[`${mode}_threshold_ms`]
const thresholdMetric = computed(() => startupThresholdMetric(props.summary))
const evidenceUrl = path => typeof path === 'string' && path.startsWith('reports/fastbot/')
    ? api.getReportAssetUrl(path.replace(/^reports\//, '')) : ''
</script>

<template>
    <el-alert v-if="!v2" type="info" :closable="false" title="历史 Android 启动报告" description="保留原来的计时与成功口径；未验证真实冷热状态。就绪超时可能未影响旧版成功统计，请结合轮次明细判断。" />
    <el-alert v-if="isStartupActive(task)" type="info" :closable="false" title="任务仍在运行，当前显示已完成轮次的部分结果" description="任务状态与性能达标分别统计；取消或异常中断后，已完成的轮次仍会保留。" />
    <el-alert v-if="summary.sync_status && summary.sync_status !== 'SYNCED'" type="warning" :closable="false" :title="`报告同步：${summary.sync_status}`" :description="summary.sync_error || '测试结果已保存在执行节点，等待同步。'" />
    <el-alert v-if="task.sync_state === 'pending'" type="warning" :closable="false" title="结果待同步" description="采集结果已保存在本机，连接恢复后将继续上传；这里显示本地结果。" />
    <div class="summary-cards">
        <StatCard label="任务状态"><el-tag :type="runStatusTagType(task.status)" effect="plain" disable-transitions>{{ task.cancel_requested && isStartupActive(task) ? '取消中' : runStatusLabel(task.status) }}</el-tag></StatCard>
        <StatCard label="请求 / 完成轮次" :value="`${counts.requested} / ${counts.completed}`" />
        <StatCard label="有效 / 无效样本" :value="`${counts.valid ?? '未校验'} / ${counts.invalid ?? '未校验'}`" />
        <StatCard label="失败轮次" tone="danger" :value="counts.failed" />
        <StatCard label="启动成功" :value="verdicts.launch" />
        <StatCard label="业务就绪成功" :value="verdicts.ready" />
        <StatCard label="性能达标 / 已评估" :value="verdicts.performance" />
        <StatCard label="超阈值次数" tone="warning" :value="summary.slow_count ?? 0" />
    </div>
    <el-card shadow="never" class="info-card">
        <template #header><span class="card-title">启动测试配置与计时口径</span></template>
        <el-descriptions :column="3" border size="small" class="config-descriptions">
            <el-descriptions-item :label="platform === 'ios' ? 'Bundle ID' : '包名'"><span class="mono-text">{{ task.package_name }}</span></el-descriptions-item>
            <el-descriptions-item label="平台 / 执行节点">{{ startupPlatformLabel(platform) }} / {{ startupExecutorLabel(task) }}</el-descriptions-item>
            <el-descriptions-item label="设备">{{ deviceName }}</el-descriptions-item>
            <el-descriptions-item v-if="platform === 'android'" label="Activity"><span class="mono-text">{{ startupConfig.resolved_component || startupConfig.activity_name || '—' }}</span></el-descriptions-item>
            <el-descriptions-item label="测量版本">{{ summary.measurement_version || '历史版本' }}</el-descriptions-item>
            <el-descriptions-item label="启动模式">{{ startupModesLabel }}</el-descriptions-item>
            <el-descriptions-item label="每模式轮次">{{ startupConfig.iterations ?? task.duration ?? '—' }}</el-descriptions-item>
            <el-descriptions-item label="轮次间隔">{{ startupConfig.cooldown_sec ?? '—' }}s</el-descriptions-item>
            <el-descriptions-item label="阈值指标">{{ startupMetricLabel(thresholdMetric, platform) }}</el-descriptions-item>
            <el-descriptions-item label="冷启动阈值"><span class="num-cell">{{ formatMs(threshold('cold')) }}</span></el-descriptions-item>
            <el-descriptions-item label="后台恢复阈值"><span class="num-cell">{{ formatMs(threshold('hot')) }}</span></el-descriptions-item>
            <el-descriptions-item label="业务就绪检查">{{ startupConfig.ready_check?.enabled ? '已开启' : '未开启' }}</el-descriptions-item>
            <el-descriptions-item v-if="platform === 'android'" label="Perfetto 取证">{{ startupConfig.perfetto_slow_trace?.enabled ? (v2 ? '已开启（原轮次取证）' : '已开启（诊断复跑）') : '未开启' }}</el-descriptions-item>
            <el-descriptions-item label="开始时间"><span class="num-cell">{{ formatTime(task.started_at) }}</span></el-descriptions-item>
            <el-descriptions-item label="结束时间"><span class="num-cell">{{ formatTime(task.finished_at) }}</span></el-descriptions-item>
            <el-descriptions-item label="执行人">{{ task.executor_name || '—' }}</el-descriptions-item>
        </el-descriptions>
        <div v-if="summary.error" class="task-notice">任务说明：{{ summary.error }}</div>
        <details class="methodology-box">
            <summary>计时口径与指标来源说明</summary>
            <ul class="methodology-list">
                <li v-if="v2">{{ startupMetricDescription('automation_ms', platform) }}</li>
                <li>{{ startupMetricDescription('ready_ms', platform) }} 数值缺失表示未采集或不适用，真实 0 ms 会保留。</li>
                <li v-if="platform === 'android'">{{ startupMetricDescription('total_time_ms', platform) }}</li>
                <li v-for="metadata in metricMetadata" :key="metadata.key">
                    <strong>{{ metadata.label }}</strong>（{{ metadata.source }} · 单位 {{ metadata.unit }}）：{{ metadata.description }}
                </li>
            </ul>
        </details>
    </el-card>
    <el-card shadow="never" class="events-card">
        <template #header>
            <div class="section-header">
                <span class="card-title">分模式与指标统计</span>
                <span class="header-hint">仅统计有效正式样本（诊断复跑不参与）；不足 20 样本标注样本不足</span>
            </div>
        </template>
        <el-table :data="metricRows" class="report-table">
            <el-table-column label="模式" min-width="136">
                <template #default="{ row }">{{ formatStartupMode(row.mode) }}</template>
            </el-table-column>
            <el-table-column label="指标来源" prop="label" min-width="156" show-overflow-tooltip />
            <el-table-column label="有效样本" min-width="124">
                <template #default="{ row }">
                    <template v-if="row.count === null || row.count === undefined">
                        <span class="tone-muted">历史报告未记录</span>
                    </template>
                    <span v-else class="sample-cell">
                        <span class="num-cell">{{ row.count }}</span>
                        <span v-if="isSampleInsufficient(row)" class="sample-badge">样本不足</span>
                    </span>
                </template>
            </el-table-column>
            <el-table-column
                v-for="column in [{ key: 'min_ms', label: '最小' }, { key: 'p50_ms', label: 'P50' }, { key: 'p90_ms', label: 'P90', emphasis: true }, { key: 'p95_ms', label: 'P95' }, { key: 'max_ms', label: '最大' }, { key: 'avg_ms', label: '平均' }]"
                :key="column.key"
                :label="column.label"
                min-width="76"
                align="right"
            >
                <template #default="{ row }">
                    <span class="num-cell" :class="{ 'num-emphasis': column.emphasis }">{{ formatMs(row[column.key]) }}</span>
                </template>
            </el-table-column>
        </el-table>
    </el-card>
    <el-card shadow="never" class="events-card">
        <template #header><span class="card-title">启动轮次明细 ({{ startupRuns.length }})</span></template>
        <el-table v-if="startupRuns.length" :data="startupRuns" class="report-table runs-table">
            <el-table-column type="expand" width="40">
                <template #default="{ row }">
                    <div class="expand-panel">
                        <el-descriptions :column="2" border size="small" class="run-details">
                            <el-descriptions-item label="启动前状态">{{ startupStateText(row.state_before) }}</el-descriptions-item>
                            <el-descriptions-item label="启动后状态">{{ startupStateText(row.state_after ?? row.state_post) }}</el-descriptions-item>
                            <el-descriptions-item label="计时来源">{{ startupMetricSourceText(row.metric_sources, platform) }}</el-descriptions-item>
                            <el-descriptions-item label="错误 / 无效原因">{{ row.invalid_reason || row.error || row.ready_error || '—' }}</el-descriptions-item>
                            <template v-if="platform === 'android'">
                                <el-descriptions-item label="ThisTime"><span class="num-cell">{{ formatMs(row.this_time_ms) }}</span></el-descriptions-item>
                                <el-descriptions-item label="WaitTime"><span class="num-cell">{{ formatMs(row.wait_time_ms) }}</span></el-descriptions-item>
                                <el-descriptions-item label="Displayed"><span class="num-cell">{{ formatMs(row.displayed?.time_ms) }}</span></el-descriptions-item>
                                <el-descriptions-item label="Fully drawn"><span class="num-cell">{{ formatMs(row.fully_drawn?.time_ms) }}</span></el-descriptions-item>
                            </template>
                        </el-descriptions>
                    </div>
                </template>
            </el-table-column>
            <el-table-column label="轮次" prop="iteration" width="48" align="center" />
            <el-table-column label="模式（请求 / 实际）" min-width="126">
                <template #default="{ row }">
                    <div class="mode-primary">{{ formatStartupMode(row.requested_mode || row.mode) }}</div>
                    <small class="cell-sub" :class="{ 'tone-warning': modeMismatch(row) }">实际：{{ row.actual_mode ? formatStartupMode(row.actual_mode) : '未验证' }}</small>
                </template>
            </el-table-column>
            <el-table-column label="状态" width="76" align="center">
                <template #default="{ row }">
                    <el-tag :type="row.status === 'INVALID' ? 'warning' : runStatusTagType(row.status)" size="small" effect="plain">{{ row.status === 'INVALID' ? '样本无效' : runStatusLabel(row.status) }}</el-tag>
                </template>
            </el-table-column>
            <el-table-column label="样本" width="76" align="center">
                <template #default="{ row }">
                    <el-tooltip :content="row.invalid_reason || sampleOutcome(row)">
                        <span :class="row.sample_valid === true ? 'tone-success' : 'tone-muted'">{{ shortSampleOutcome(row) }}</span>
                    </el-tooltip>
                </template>
            </el-table-column>
            <el-table-column label="启动" width="60" align="center">
                <template #default="{ row }">
                    <span :class="outcomeClass(v2 ? row.launch_success : row.success)">{{ launchOutcome(row) }}</span>
                </template>
            </el-table-column>
            <el-table-column label="就绪" width="66" align="center">
                <template #default="{ row }">{{ formatReadyStatus(row.ready_status) }}</template>
            </el-table-column>
            <el-table-column label="性能" width="64" align="center">
                <template #default="{ row }">
                    <span :class="outcomeClass(row.performance_passed)">{{ formatOutcome(row.performance_passed, '达标', '未达标') }}</span>
                </template>
            </el-table-column>
            <el-table-column v-if="v2" :label="platform === 'ios' ? 'WDA 耗时' : '自动化耗时'" width="86" align="right">
                <template #default="{ row }"><span class="num-cell">{{ formatMs(row.automation_ms) }}</span></template>
            </el-table-column>
            <el-table-column label="就绪耗时" width="78" align="right">
                <template #default="{ row }"><span class="num-cell">{{ formatMs(row.ready_ms) }}</span></template>
            </el-table-column>
            <el-table-column v-if="platform === 'android'" label="TotalTime" width="82" align="right">
                <template #default="{ row }"><span class="num-cell">{{ formatMs(row.total_time_ms) }}</span></template>
            </el-table-column>
            <el-table-column label="错误 / 无效原因" min-width="112" show-overflow-tooltip>
                <template #default="{ row }">{{ row.invalid_reason || row.error || row.ready_error || '—' }}</template>
            </el-table-column>
            <el-table-column label="证据" width="86" align="center">
                <template #default="{ row }">
                    <div v-if="evidenceUrl(row.screenshot_path) || evidenceUrl(row.log_path)" class="evidence-links">
                        <el-link v-if="evidenceUrl(row.screenshot_path)" :href="evidenceUrl(row.screenshot_path)" target="_blank" rel="noopener" type="primary">截图</el-link>
                        <el-link v-if="evidenceUrl(row.log_path)" :href="evidenceUrl(row.log_path)" target="_blank" rel="noopener" type="primary">日志</el-link>
                    </div>
                    <span v-else class="tone-muted">{{ row.screenshot_path || row.log_path ? '待同步' : '未采集' }}</span>
                </template>
            </el-table-column>
        </el-table>
        <el-empty v-else :image-size="48" class="compact-empty" description="暂无已完成轮次" />
    </el-card>
    <el-card v-if="platform === 'android'" shadow="never" class="events-card">
        <template #header><span class="card-title">慢启动事件 ({{ slowEvents.length }})</span></template>
        <el-table v-if="slowEvents.length" :data="slowEvents" class="report-table">
            <el-table-column label="模式" min-width="140"><template #default="{ row }">{{ formatStartupMode(row.mode) }}</template></el-table-column>
            <el-table-column label="轮次" prop="iteration" width="66" align="center" />
            <el-table-column label="耗时" width="104" align="right"><template #default="{ row }"><span class="num-cell">{{ formatMs(row.metric_value_ms ?? row.total_time_ms) }}</span></template></el-table-column>
            <el-table-column label="阈值" width="104" align="right"><template #default="{ row }"><span class="num-cell">{{ formatMs(row.threshold_ms) }}</span></template></el-table-column>
            <el-table-column label="Trace" min-width="240" show-overflow-tooltip><template #default="{ row }"><span class="mono-text">{{ row.trace_path || row.trace_error || '未导出' }}</span></template></el-table-column>
            <el-table-column label="诊断状态" width="110" align="center"><template #default="{ row }">{{ formatDiagnosisStatus(row.diagnosis_status) }}</template></el-table-column>
        </el-table>
        <el-empty v-else :image-size="48" class="compact-empty" description="没有触发慢启动阈值" />
    </el-card>
    <el-card v-if="platform === 'android'" shadow="never" class="events-card">
        <template #header>
            <div class="trace-header">
                <div class="section-header">
                    <span class="card-title">Perfetto Trace ({{ traceArtifacts.length }})</span>
                    <span class="header-hint">展开查看原轮次启动区间、主线程切片与调度证据（仅描述所选区间）</span>
                </div>
                <el-button
                    v-if="legacyAnalyzedCount > 0"
                    type="primary"
                    link
                    :loading="batchLoading"
                    :disabled="legacyAnalyzedCount === 0"
                    @click="$emit('generate-summaries')"
                >
                    全部生成 AI 总结
                </el-button>
            </div>
        </template>
        <el-table
            v-if="traceArtifacts.length > 0"
            :data="traces"
            class="report-table"
        >
            <el-table-column type="expand" width="40">
                <template #default="{ row }">
                    <div v-if="row.startupEvidence" class="trace-evidence">
                        <el-alert v-if="row.startupEvidence.status === 'PARTIAL'" type="warning" :closable="false" title="部分分析：证据有限" description="启动事件或调度数据不完整，以下仅展示已取得的证据，不能据此判定完整启动耗时或根因。" class="trace-alert" />
                        <div class="evidence-meta-bar">
                            <span>分析区间来源：<strong>{{ row.startupEvidence.source }}</strong></span>
                            <span>区间长度：<strong class="num-cell">{{ formatMs(row.startupEvidence.window_ms) }}</strong></span>
                            <span class="tone-muted">时长统一换算为 ms</span>
                        </div>
                        <p v-if="row.startupEvidence.reason" class="metric-note">{{ row.startupEvidence.reason }}</p>
                        <p v-for="(limitation, index) in row.startupEvidence.limitations" :key="index" class="metric-note">{{ limitation }}</p>
                        <h4 class="evidence-title">启动区间（Android 启动事件）</h4>
                        <el-table :data="row.startupEvidence.startup_intervals" empty-text="未取得目标应用的启动事件" class="nested-table">
                            <el-table-column label="启动编号" prop="startup_id" width="84" align="center" />
                            <el-table-column label="应用" prop="package" min-width="200" show-overflow-tooltip />
                            <el-table-column label="实际类型" min-width="140"><template #default="{ row: interval }">{{ formatStartupMode(interval.startup_type) }}</template></el-table-column>
                            <el-table-column label="启动耗时" width="116" align="right"><template #default="{ row: interval }"><span class="num-cell">{{ formatMs(interval.duration_ms) }}</span></template></el-table-column>
                            <el-table-column label="相对起点" width="116" align="right"><template #default="{ row: interval }"><span class="num-cell">{{ formatMs(interval.offset_ms) }}</span></template></el-table-column>
                            <el-table-column label="线程证据范围" width="128" align="center"><template #default="{ row: interval }">{{ interval.selected ? '本次选定区间' : '其他启动事件' }}</template></el-table-column>
                        </el-table>
                        <h4 class="evidence-title">主线程切片</h4>
                        <el-table :data="row.startupEvidence.main_thread_slices" empty-text="未取得主线程切片" class="nested-table">
                            <el-table-column label="进程" prop="process_name" min-width="190" show-overflow-tooltip />
                            <el-table-column label="TID" prop="tid" width="76" align="right" />
                            <el-table-column label="切片名称" prop="name" min-width="240" show-overflow-tooltip />
                            <el-table-column label="累计时长" width="120" align="right"><template #default="{ row: slice }"><span class="num-cell">{{ formatMs(slice.duration_ms) }}</span></template></el-table-column>
                            <el-table-column label="出现次数" prop="occurrences" width="88" align="right" />
                        </el-table>
                        <h4 class="evidence-title">CPU 运行时间（按线程）</h4>
                        <el-table :data="row.startupEvidence.cpu_running" empty-text="未取得线程 CPU 调度数据" class="nested-table">
                            <el-table-column label="进程" prop="process_name" min-width="190" show-overflow-tooltip />
                            <el-table-column label="线程" prop="thread_name" min-width="190" show-overflow-tooltip />
                            <el-table-column label="TID" prop="tid" width="76" align="right" />
                            <el-table-column label="CPU 时长" width="120" align="right"><template #default="{ row: thread }"><span class="num-cell">{{ formatMs(thread.running_ms) }}</span></template></el-table-column>
                            <el-table-column label="调度片段数" prop="sched_slices" width="96" align="right" />
                        </el-table>
                        <h4 class="evidence-title">主线程调度状态</h4>
                        <p class="metric-note">保留 Trace Processor 状态代码：Running 运行中、R 可运行、S 可中断睡眠、D 不可中断睡眠；其他代码按原值展示。</p>
                        <el-table :data="row.startupEvidence.main_thread_states" empty-text="未取得主线程调度状态" class="nested-table">
                            <el-table-column label="进程" prop="process_name" min-width="190" show-overflow-tooltip />
                            <el-table-column label="TID" prop="tid" width="76" align="right" />
                            <el-table-column label="状态代码" prop="state" min-width="110" />
                            <el-table-column label="累计时长" width="120" align="right"><template #default="{ row: state }"><span class="num-cell">{{ formatMs(state.duration_ms) }}</span></template></el-table-column>
                            <el-table-column label="状态区间数" prop="intervals" width="96" align="right" />
                        </el-table>
                    </div>
                    <p v-else class="metric-note expand-empty">{{ row.analysis_error || row.analysis_note || '暂无启动区间结构化证据；请结合分析状态或原始 Trace 查看。' }}</p>
                </template>
            </el-table-column>
            <el-table-column label="触发时间" prop="trigger_time" width="92" align="center" />
            <el-table-column label="模式" min-width="140">
                <template #default="{ row }">{{ formatStartupMode(row.startup_mode) }}</template>
            </el-table-column>
            <el-table-column label="诊断状态" width="90" align="center">
                <template #default="{ row }">{{ formatTraceAnalysisStatus(row.analysis_status) }}</template>
            </el-table-column>
            <el-table-column label="分析说明" min-width="200" show-overflow-tooltip>
                <template #default="{ row }">{{ row.startupEvidence ? (row.startupEvidence.status === 'PARTIAL' ? '证据有限，请展开查看已取得数据' : row.startupEvidence.reason || '启动区间证据，请展开查看') : row.analysis_error || row.analysis_note || getPrimaryTraceCause(row) }}</template>
            </el-table-column>
            <el-table-column label="Trace 路径" min-width="200" prop="path" show-overflow-tooltip>
                <template #default="{ row }"><span class="mono-text">{{ row.path }}</span></template>
            </el-table-column>
            <el-table-column label="AI 总结" width="104" align="center">
                <template #default="{ row }">
                    <span v-if="row.startupOnly || row.startupEvidence" class="tone-muted">展开查看证据</span>
                    <el-button
                        v-else
                        link
                        type="primary"
                        :disabled="row.analysis_status !== 'ANALYZED'"
                        @click="$emit('open-trace-ai', row.sourceArtifact)"
                    >
                        {{ row.sourceArtifact.ai_summary ? '查看' : '生成' }}
                    </el-button>
                </template>
            </el-table-column>
        </el-table>
        <el-empty v-else :image-size="48" class="compact-empty" description="暂无慢启动 Perfetto Trace" />
    </el-card>
</template>

<style scoped>
.summary-cards {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
}

.summary-cards :deep(.stat-card) {
    min-width: 0;
}

.info-card,
.events-card {
    border-radius: var(--ad-radius);
}

.section-header,
.trace-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 6px 12px;
}

.section-header {
    justify-content: flex-start;
}

.header-hint {
    color: var(--ad-muted);
    font-size: 12px;
    font-weight: 400;
}

.card-title {
    font-size: 13px;
    font-weight: 600;
    color: var(--ad-text);
}

.config-descriptions :deep(.el-descriptions__label) {
    white-space: nowrap;
}

.config-descriptions :deep(.el-descriptions__content) {
    word-break: break-word;
}

.task-notice {
    margin-top: 10px;
    padding: 8px 12px;
    border-radius: 4px;
    border-left: 3px solid var(--ad-warning);
    background: var(--ad-warning-soft);
    color: var(--ad-text);
    font-size: 12px;
    line-height: 1.55;
}

.methodology-box {
    margin-top: 12px;
    padding: 8px 12px;
    border-radius: var(--ad-radius);
    background: var(--ad-bg);
    border: 1px solid var(--ad-border);
}

.methodology-box summary {
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
    color: var(--ad-muted);
    user-select: none;
}

.methodology-list {
    margin: 8px 0 2px;
    padding-left: 16px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    color: var(--ad-muted);
    font-size: 12px;
    line-height: 1.55;
}

.report-table :deep(.el-table__cell) {
    padding: 7px 0;
    height: auto;
}

.report-table :deep(.cell) {
    line-height: 1.45;
}

.runs-table :deep(.el-table__expanded-cell) {
    padding: 0;
    background: transparent;
}

.expand-panel {
    position: sticky;
    left: 0;
    max-width: min(100%, 1080px);
    margin: 8px 12px;
    padding: 12px 14px;
    border-radius: var(--ad-radius);
    background: var(--ad-bg);
    border: 1px solid var(--ad-border);
}

.run-details {
    margin: 0;
}

.run-details :deep(.el-descriptions__label) {
    width: 118px;
    white-space: nowrap;
}

.trace-evidence {
    position: sticky;
    left: 0;
    max-width: min(100%, 1160px);
    margin: 8px 12px 12px;
    padding: 14px 16px;
    border-radius: var(--ad-radius);
    background: var(--ad-bg);
    border: 1px solid var(--ad-border);
}

.trace-alert {
    margin-bottom: 10px;
}

.evidence-meta-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 20px;
    padding: 8px 12px;
    border-radius: var(--ad-radius);
    background: var(--ad-surface);
    border: 1px solid var(--ad-border);
    font-size: 12px;
    color: var(--ad-text);
    margin-bottom: 8px;
}

.nested-table {
    border: 1px solid var(--ad-border);
    border-radius: var(--ad-radius);
    overflow: hidden;
}

.evidence-title {
    margin: 14px 0 6px;
    font-size: 12px;
    font-weight: 600;
    color: var(--ad-text);
}

.expand-empty {
    margin: 10px 16px;
}

.metric-note {
    color: var(--ad-muted);
    font-size: 12px;
    line-height: 1.6;
    margin: 6px 0;
}

.compact-empty {
    padding: 12px 0;
}

.sample-cell {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    white-space: nowrap;
}

.sample-badge {
    padding: 1px 6px;
    border-radius: 4px;
    background: var(--ad-warning-soft);
    color: var(--ad-warning);
    font-size: 11px;
    line-height: 1.4;
}

.mode-primary {
    color: var(--ad-text);
}

.cell-sub {
    display: block;
    margin-top: 2px;
    color: var(--ad-muted);
    font-size: 11.5px;
}

.evidence-links {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    flex-wrap: wrap;
}

.mono-text {
    font-family: Menlo, Monaco, Consolas, monospace;
}

.num-cell {
    font-variant-numeric: tabular-nums;
}

.num-emphasis {
    font-weight: 600;
    color: var(--ad-text);
}

.tone-success { color: var(--ad-success); }
.tone-danger { color: var(--ad-danger); font-weight: 500; }
.tone-warning { color: var(--ad-warning); }
.tone-muted { color: var(--ad-muted); }

@media (max-width: 960px) {
    .summary-cards {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
}

@media (max-width: 767px) {
    :deep(.el-button), :deep(.el-radio-button__inner), :deep(.el-select__wrapper), :deep(.el-collapse-item__header) { min-height: 44px; }
    :deep(.el-input__inner), :deep(.el-textarea__inner) { font-size: 16px; }
    :deep(.el-form-item__label), :deep(.el-table), :deep(.el-descriptions), :deep(.el-tabs__item), :deep(.el-collapse-item__content) { font-size: 14px; }
    :deep(.el-table__cell) { font-size: 14px; }
}
</style>
