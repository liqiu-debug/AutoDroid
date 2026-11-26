import { createUuid } from './uuid.js'

export const STARTUP_PROTOCOL_VERSION = 1
export const STARTUP_ACTIVE_STATUSES = ['PENDING', 'QUEUED', 'CLAIMED', 'RUNNING']
export const STARTUP_UPGRADE_HINT = '当前桌面客户端不支持启动测试，请升级客户端后重试'

export const startupNumber = value => {
    if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null
    const number = Number(value)
    return Number.isFinite(number) ? number : null
}

export const formatStartupMs = value => {
    const number = startupNumber(value)
    return number === null ? '未采集' : `${number} ms`
}

export const startupPlatform = value => String(value || 'android').toLowerCase()
export const startupPlatformLabel = value => startupPlatform(value) === 'ios' ? 'iOS' : 'Android'
export const startupModeLabel = value => ({ cold: '冷启动', hot: '热启动（后台恢复）' })[value] || value || '—'
export const startupExecutorLabel = task => ['desktop', 'client'].includes(task?.executor_kind) ? '桌面客户端' : '服务端'
export const isStartupActive = task => STARTUP_ACTIVE_STATUSES.includes(String(task?.status || '').toUpperCase())
export const startupSummary = value => value?.summary || value || {}
export const isStartupV2 = value => startupSummary(value).measurement_version === 'startup-v2'

export const startupDeviceReason = (device, { web = false } = {}) => {
    const capabilities = device?.capabilities
    if (!capabilities || !(Number(capabilities.protocol_version) >= STARTUP_PROTOCOL_VERSION)) {
        return device?.unavailable_reason || device?.reason || (['desktop', 'client'].includes(device?.executor_kind)
            ? STARTUP_UPGRADE_HINT : '执行节点尚未提供启动测试能力')
    }
    const platform = startupPlatform(device.platform)
    if (!Array.isArray(capabilities.platforms) || !capabilities.platforms.includes(platform)) return '执行节点不支持此平台的启动测试'
    if (web && ['desktop', 'client'].includes(device.executor_kind) && capabilities.web_dispatch !== true) return '该客户端不支持 Web 派发，请升级客户端'
    if (device.available !== true) return device.unavailable_reason || device.reason || '设备当前不可用于启动测试'
    return ''
}

const capabilityValues = (device, name) => device?.capabilities?.[`${name}_by_platform`]?.[startupPlatform(device.platform)] ?? device?.capabilities?.[name]
const capabilityIntersection = (devices, name) => {
    if (!devices.length) return []
    const first = capabilityValues(devices[0], name)
    const values = Array.isArray(first) ? first : []
    return values.filter(value => devices.every(device => {
        const supported = capabilityValues(device, name)
        return Array.isArray(supported) && supported.includes(value)
    }))
}

export const startupReadyLocators = devices => capabilityIntersection(devices, 'ready_locators')
export const startupMetrics = devices => capabilityIntersection(devices, 'metrics')
export const startupTraceAvailable = devices => devices.length > 0 && devices.every(device => {
    const trace = device.capabilities?.trace
    return trace === true || trace === 'perfetto' || (Array.isArray(trace) && trace.includes('perfetto')) || trace?.perfetto === true || trace?.available === true
})

export const startupMetricLabel = (metric, platform = 'android') => ({
    total_time_ms: 'Android 原生 TotalTime',
    automation_ms: platform === 'ios' ? 'WDA 启动 / 恢复耗时' : '自动化启动 / 恢复耗时',
    ready_ms: '业务就绪耗时',
})[metric] || metric

export const startupMetricDescription = (metric, platform) => ({
    total_time_ms: 'Android am start -W 返回的系统计时；不等同于业务首页完全可用。',
    automation_ms: platform === 'ios' ? '从 WDA 发起 launch / activate 至返回并确认前台，包含 WDA 控制与状态查询开销。' : '从主机发起启动 / 恢复命令至返回并确认前台，包含控制链路开销。',
    ready_ms: '从发起启动 / 恢复至目标元素命中，包含自动化查询与采样开销。',
})[metric] || ''

const evidenceText = value => typeof value === 'string' ? value : ''
const nonnegativeNumber = value => {
    const number = startupNumber(value)
    return number !== null && number >= 0 ? number : null
}
const traceMs = value => {
    const number = nonnegativeNumber(value)
    return number === null ? null : Math.round(number / 1000) / 1000
}

// Project display fields explicitly. Runtime tool/auth metadata and unknown keys
// never become raw JSON in reports.
export const startupStateText = value => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return '未记录'
    const states = { not_running: '未运行', foreground: '前台', background: '后台', unknown: '未知', suspended: '后台挂起' }
    const iosStates = { 1: '未运行', 2: '后台挂起', 3: '后台', 4: '前台' }
    const parts = [states[value.state] || iosStates[value.value] || '未知']
    const pids = Array.isArray(value.pids) ? value.pids.filter(pid => Number.isInteger(pid) && pid > 0) : []
    if (pids.length) parts.push(`PID ${pids.join(', ')}`)
    else if (Number.isInteger(value.process_id) && value.process_id > 0) parts.push(`PID ${value.process_id}`)
    if (value.process_id_source === 'preparation_foreground') parts.push('PID 来自准备阶段前台查询')
    else if (value.process_id_source === 'unavailable') parts.push('未取得 PID')
    return parts.join(' · ')
}

export const startupMetricMetadata = (summary, platform) => Object.entries(summary?.metrics_metadata || {})
    .filter(([key, value]) => ['automation_ms', 'total_time_ms', 'ready_ms'].includes(key) && value && typeof value === 'object')
    .map(([key, value]) => ({
        key, label: evidenceText(value.label) || startupMetricLabel(key, platform),
        source: evidenceText(value.source) || '未记录来源',
        unit: evidenceText(value.unit) || '未记录单位', description: evidenceText(value.description),
    }))

export const startupMetricSourceText = (sources, platform) => ['automation_ms', 'total_time_ms', 'ready_ms']
    .filter(key => typeof sources?.[key] === 'string')
    .map(key => `${startupMetricLabel(key, platform)}：${sources[key]}`)
    .join('；') || '未记录'

export const startupTraceEvidence = artifact => {
    const analysis = artifact?.analysis
    if (!analysis || typeof analysis !== 'object'
        || (analysis.analysis_level !== 'startup_evidence' && !Array.isArray(analysis.startup_intervals))) return null
    const start = startupNumber(analysis.window?.start_ns)
    const end = startupNumber(analysis.window?.end_ns)
    const rows = (key, project) => Array.isArray(analysis[key])
        ? analysis[key].filter(value => value && typeof value === 'object').map(project) : []
    return {
        status: artifact.analysis_status || analysis.analysis_status,
        source: ({ 'android.startup.startups': 'Android 启动事件', trace_bounds_fallback: '整段录制区间（缺少启动事件）' })[analysis.window_source] || '未记录来源',
        window_ms: start !== null && end !== null && end >= start ? traceMs(end - start) : null,
        selected_startup_id: analysis.selected_startup_id,
        reason: evidenceText(analysis.reason),
        limitations: Array.isArray(analysis.limitations) ? analysis.limitations.filter(value => typeof value === 'string') : [],
        startup_intervals: rows('startup_intervals', row => ({
            startup_id: nonnegativeNumber(row.startup_id), package: evidenceText(row.package), startup_type: evidenceText(row.startup_type),
            duration_ms: traceMs(row.dur), selected: nonnegativeNumber(analysis.selected_startup_id) !== null && row.startup_id === analysis.selected_startup_id,
            offset_ms: start !== null && startupNumber(row.ts) !== null ? Math.round((startupNumber(row.ts) - start) / 1000) / 1000 : null,
        })),
        main_thread_slices: rows('main_thread_slices', row => ({
            process_name: evidenceText(row.process_name), tid: nonnegativeNumber(row.tid), name: evidenceText(row.name),
            duration_ms: nonnegativeNumber(row.duration_ms), occurrences: nonnegativeNumber(row.occurrences),
        })),
        cpu_running: rows('cpu_running', row => ({
            process_name: evidenceText(row.process_name), thread_name: evidenceText(row.thread_name), tid: nonnegativeNumber(row.tid),
            running_ms: nonnegativeNumber(row.running_ms), sched_slices: nonnegativeNumber(row.sched_slices),
        })),
        main_thread_states: rows('main_thread_states', row => ({
            process_name: evidenceText(row.process_name), tid: nonnegativeNumber(row.tid), state: evidenceText(row.state),
            duration_ms: nonnegativeNumber(row.duration_ms), intervals: nonnegativeNumber(row.intervals),
        })),
    }
}

export const startupCounts = value => {
    const summary = startupSummary(value)
    const config = summary.startup_config || {}
    const runs = (Array.isArray(summary.startup_runs) ? summary.startup_runs : []).filter(run => !run.diagnostic && run.completed !== false && run.status !== 'ABORTED')
    const progress = value?.progress || summary.progress || {}
    const count = (key, fallback) => startupNumber(summary[key]) ?? startupNumber(progress[key]) ?? fallback
    const completed = count('completed_count', runs.length || ((startupNumber(summary.success_count) ?? 0) + (startupNumber(summary.fail_count) ?? 0)))
    return {
        requested: count('requested_count', (config.startup_modes?.length || 0) * (startupNumber(config.iterations) ?? 0)),
        completed,
        valid: count('valid_count', isStartupV2(summary) ? runs.filter(run => run.sample_valid === true).length : null),
        invalid: count('invalid_count', isStartupV2(summary) ? runs.filter(run => run.status === 'INVALID' || run.sample_valid === false).length : null),
        failed: count('fail_count', runs.filter(run => run.status === 'FAIL' || (!run.status && run.success === false)).length),
    }
}

export const startupProgressText = task => {
    const counts = startupCounts(task)
    if (counts.requested > 0) return `${counts.completed}/${counts.requested}`
    return isStartupActive(task) ? '等待进度' : '—'
}

export const startupCanViewReport = task => Boolean(task?.report_ready || startupCounts(task).completed > 0 || ['COMPLETED', 'FAILED', 'ABORTED'].includes(task?.status))

export const mergeStartupTasks = (remote, local) => {
    const merged = new Map((Array.isArray(remote) ? remote : []).map(task => [String(task.id), { ...task }]))
    for (const task of Array.isArray(local) ? local : []) {
        if (task?.id === null || task?.id === undefined) continue
        const key = String(task.id)
        const existing = merged.get(key)
        if (existing && task.sync_state === 'synced') continue
        if (!existing && remote.length > 0 && task.sync_state === 'synced') continue
        const summary = task.summary || task.report?.summary || existing?.summary
        merged.set(key, { ...existing, ...task, ...(summary ? { summary } : {}) })
    }
    return [...merged.values()].sort((a, b) => Number(b.id) - Number(a.id))
}

export const startupMetricStats = (value, mode, metric) => {
    const summary = startupSummary(value)
    const aggregate = summary.startup_aggregate?.[mode] || {}
    if (aggregate.metrics?.[metric]) return aggregate.metrics[metric]
    if (isStartupV2(summary) || metric === 'automation_ms') return null
    if (metric === 'total_time_ms') return { count: aggregate.success_count ?? aggregate.count, min_ms: aggregate.min_ms, p50_ms: aggregate.median_ms, p90_ms: aggregate.p90_ms, p95_ms: aggregate.p95_ms, max_ms: aggregate.max_ms, avg_ms: aggregate.avg_ms, slow_count: aggregate.slow_count }
    if (metric === 'ready_ms') return { count: aggregate.ready_count, min_ms: aggregate.ready_min_ms, p50_ms: aggregate.ready_median_ms, p90_ms: aggregate.ready_p90_ms, p95_ms: aggregate.ready_p95_ms, max_ms: aggregate.ready_max_ms, avg_ms: aggregate.ready_avg_ms }
    return null
}

export const startupPercentileText = (value, mode, metric, percentile = 'p90_ms') => {
    const stats = startupMetricStats(value, mode, metric)
    const number = startupNumber(stats?.[percentile])
    if (number === null) return '未采集'
    const count = startupNumber(stats?.count)
    return `${number} ms${stats?.samples_sufficient === false || (count !== null && count < 20) ? ' · 样本不足' : ''}`
}

export const startupThresholdMetric = value => {
    const summary = startupSummary(value)
    return summary.startup_config?.thresholds?.metric || (startupPlatform(summary.platform || summary.startup_config?.platform) === 'ios' ? 'automation_ms' : 'total_time_ms')
}

export const startupVerdicts = value => {
    const summary = startupSummary(value)
    const runs = (Array.isArray(summary.startup_runs) ? summary.startup_runs : []).filter(run => !run.diagnostic && run.completed !== false && run.status !== 'ABORTED')
    const v2 = isStartupV2(summary)
    const launchCount = runs.filter(run => (v2 ? run.launch_success : run.success) === true).length
    const readyEnabled = summary.startup_config?.ready_check?.enabled === true
    const readyCount = runs.filter(run => run.ready_status === 'FOUND').length
    const evaluated = runs.filter(run => typeof run.performance_passed === 'boolean')
    return {
        launch: runs.length ? `${launchCount}/${runs.length}` : '未执行',
        ready: readyEnabled ? (runs.length ? `${readyCount}/${runs.length}` : '未执行') : '未开启',
        performance: evaluated.length ? `${evaluated.filter(run => run.performance_passed).length}/${evaluated.length}` : '未评估',
    }
}

export const startupRequestId = createUuid

// A request with no definitive HTTP response may already be running. Keep its
// immutable payload and request_id until the same request is acknowledged.
export const createStartupSubmission = (requestId = startupRequestId) => {
    let pending = null
    return {
        prepare(payload) {
            if (!pending) pending = { ...JSON.parse(JSON.stringify(payload)), request_id: requestId() }
            return JSON.parse(JSON.stringify(pending))
        },
        resolve() { pending = null },
        peek: () => pending === null ? null : JSON.parse(JSON.stringify(pending)),
        reject(error) {
            const status = error?.response?.status
            if (status >= 400 && status < 500 && ![408, 409, 425, 429].includes(status)) pending = null
            return pending !== null
        },
        hasPending: () => pending !== null,
    }
}

const sessionSubmissions = new Map()
export const startupSubmissionForSession = sessionKey => {
    const key = String(sessionKey)
    if (!sessionSubmissions.has(key)) sessionSubmissions.set(key, createStartupSubmission())
    return sessionSubmissions.get(key)
}
