import dayjs from 'dayjs'

/**
 * Fastbot 报告展示层的纯格式化 / 派生函数集合。
 */

export const formatPercent = (value) => `${((Number(value) || 0) * 100).toFixed(1)}%`

export const formatMetric = (value, digits = 1) => {
    if (value === null || value === undefined || value === '') return '-'
    const num = Number(value)
    return Number.isFinite(num) ? num.toFixed(digits) : '-'
}

export const formatMs = (value) => {
    const num = Number(value)
    return Number.isFinite(num) ? `${num} ms` : '-'
}

export const formatStartupMode = (value) => value === 'cold' ? '冷启动' : (value === 'hot' ? '热启动' : value || '-')

export const formatReadyStatus = (value) => {
    const map = {
        DISABLED: '未开启',
        SKIPPED: '未配置',
        FOUND: '已就绪',
        TIMEOUT: '超时',
        ERROR: '异常',
    }
    return map[value] || value || '-'
}

export const pickMedianMetric = (values) => {
    const numbers = values
        .map(value => Number(value))
        .filter(value => Number.isFinite(value) && value > 0)
        .sort((a, b) => a - b)
    if (numbers.length === 0) return null
    const mid = Math.floor(numbers.length / 2)
    return numbers.length % 2 === 1
        ? numbers[mid]
        : (numbers[mid - 1] + numbers[mid]) / 2
}

export const formatDiagnosisStatus = (value) => {
    const labelMap = {
        PENDING: '待分析',
        EXPORT_IN_PROGRESS: '录制中',
        ANALYZED: '已分析',
        EXPORT_FAILED: '导出失败',
        EXPORT_LIMIT_REACHED: '达到上限',
        EXPORT_COOLDOWN: '冷却中',
        ANALYSIS_FAILED: '分析失败',
        UNAVAILABLE: '未采集',
    }
    return labelMap[value] || value || '-'
}

export const formatJankSeverity = (value) => {
    const labelMap = {
        CRITICAL: '严重卡顿',
        WARNING: '轻微卡顿',
    }
    return labelMap[value] || value || '-'
}

export const formatJankReason = (value) => {
    const labelMap = {
        LOW_FPS: '帧率过低',
        HIGH_JANK_RATE: '卡顿帧占比高',
        FROZEN_FRAME: '画面冻结',
        TASK_COMPLETED: '任务结束后导出',
    }
    return labelMap[value] || value || '-'
}

export const formatJankSource = (value) => {
    const labelMap = {
        gfxinfo: '系统帧统计',
        perfetto: 'Perfetto',
    }
    return labelMap[value] || value || '-'
}

export const formatTraceAnalysisStatus = (value) => {
    const labelMap = {
        ANALYZED: '已分析',
        FAILED: '分析失败',
        TOOL_MISSING: '缺少工具',
        TRACE_MISSING: '文件缺失',
    }
    return labelMap[value] || value || '-'
}

export const getPrimaryTraceCause = (artifact) => {
    const causes = artifact?.analysis?.suspected_causes
    if (Array.isArray(causes) && causes.length > 0) {
        return causes[0]?.title || '-'
    }
    return '-'
}

export const getTopBusyThread = (artifact) => {
    const threads = artifact?.analysis?.top_busy_threads
    if (Array.isArray(threads) && threads.length > 0) {
        const thread = threads[0]
        return `${thread.thread_name || '-'} (${thread.running_ms || 0} ms)`
    }
    return '-'
}

export const getTraceAnalysisLevel = (artifact) => {
    const level = artifact?.analysis?.analysis_level
    if (level === 'full') return '完整'
    if (level === 'frame_timeline_only') return '帧级分析'
    if (level === 'partial') return '部分'
    return '-'
}

export const getTraceFrameStats = (artifact) => artifact?.analysis?.frame_stats || {}

export const getTraceCaptureMode = (artifact) => artifact?.capture_mode || 'diagnostic'

export const getTraceCaptureModeLabel = (artifact) => {
    const mode = getTraceCaptureMode(artifact)
    if (mode === 'continuous') return '全程采样'
    if (mode === 'diagnostic') return '异常诊断'
    return mode || '-'
}

export const getTraceFrameTimelineConclusion = (artifact) => {
    const stats = getTraceFrameStats(artifact)
    const cadenceFps = Number(stats.cadence_fps)
    const effectiveFps = Number(stats.effective_fps)
    const p95Delay = Number(stats.present_delay_p95_ms)

    if (
        (!Number.isFinite(cadenceFps) || cadenceFps <= 0) &&
        (!Number.isFinite(effectiveFps) || effectiveFps <= 0) &&
        (!Number.isFinite(p95Delay) || p95Delay <= 0)
    ) {
        return '-'
    }

    const fpsPart = Number.isFinite(cadenceFps) && cadenceFps > 0
        ? `${formatMetric(effectiveFps)} / ${formatMetric(cadenceFps)} Hz`
        : `${formatMetric(effectiveFps)} Hz`
    const delayPart = Number.isFinite(p95Delay)
        ? `P95 延迟 ${formatMetric(Math.max(0, p95Delay), 1)} ms`
        : 'P95 延迟 -'
    return `${fpsPart} · ${delayPart}`
}

export const getTraceTimelineSeries = (artifact) => (
    Array.isArray(artifact?.analysis?.frame_timeline_series) ? artifact.analysis.frame_timeline_series : []
)

export const formatTime = (t) => {
    if (!t) return '-'
    return dayjs(t).format('YYYY-MM-DD HH:mm:ss')
}

export const formatDurationSeconds = (value) => {
    const total = Number(value || 0)
    if (!Number.isFinite(total) || total <= 0) return '-'
    if (total < 60) return `${total}s`
    const minute = Math.floor(total / 60)
    const second = total % 60
    return `${minute}m ${second}s`
}

// ==================== 本地回放元信息 ====================

export const getReplayMeta = (event) => (
    event && typeof event.replay === 'object' && event.replay
        ? event.replay
        : null
)

export const getReplayFilename = (event) => {
    const replay = getReplayMeta(event)
    if (!replay) return ''
    if (replay.filename) return replay.filename
    const replayPath = String(replay.path || '')
    const parts = replayPath.split('/')
    return parts[parts.length - 1] || ''
}

export const isReplayReady = (event) => {
    const replay = getReplayMeta(event)
    return replay?.status === 'READY' && Boolean(getReplayFilename(event))
}

export const formatReplayStatus = (event, localReplayEnabled) => {
    const replay = getReplayMeta(event)
    if (!replay) {
        return localReplayEnabled ? '未生成回放' : '未开启录制'
    }
    if (replay.status === 'READY') {
        const durationText = Number(replay.duration_sec) > 0 ? ` · ${replay.duration_sec}s` : ''
        return `已生成${durationText}`
    }
    if (replay.status === 'UNAVAILABLE') {
        return replay.error || '未采集到可用视频'
    }
    if (replay.status === 'SKIPPED') {
        return replay.error || '回放导出已跳过'
    }
    if (replay.status === 'FAILED') {
        return replay.error || '回放生成失败'
    }
    return replay.error || replay.status || '无回放'
}

// ==================== 时间轴 / 序列换算 ====================

export const resolveReportBaseDate = (startedAt) => {
    const started = dayjs(startedAt)
    return started.isValid() ? started.startOf('day') : dayjs().startOf('day')
}

export const clockTimeToTimestamp = (baseDate, value, lastTimestamp = null) => {
    const timeText = String(value || '')
    const [hour, minute, second] = timeText.split(':').map(Number)
    if (![hour, minute, second].every(Number.isFinite)) return null

    let timestamp = baseDate
        .hour(hour)
        .minute(minute)
        .second(second)
        .millisecond(0)
        .valueOf()

    if (lastTimestamp !== null && timestamp < lastTimestamp - (12 * 3600 * 1000)) {
        timestamp += 24 * 3600 * 1000
    }
    return timestamp
}

export const formatAxisTime = (value) => dayjs(value).format('HH:mm:ss')

export const buildClockSeries = (baseDate, points, valueKey) => {
    let lastTimestamp = null
    return points
        .map((point) => {
            const timestamp = clockTimeToTimestamp(baseDate, point.time, lastTimestamp)
            if (timestamp === null) return null
            lastTimestamp = timestamp
            return [timestamp, Number(point[valueKey])]
        })
        .filter((point) => point && Number.isFinite(point[1]))
}

export const findClosestSeriesPoint = (series, targetTimestamp, maxDeltaMs = 5000) => {
    if (!Number.isFinite(targetTimestamp)) return null
    let bestPoint = null
    let minDelta = Infinity
    series.forEach((point) => {
        const delta = Math.abs(point[0] - targetTimestamp)
        if (delta < minDelta) {
            minDelta = delta
            bestPoint = point
        }
    })
    return minDelta <= maxDeltaMs ? bestPoint : null
}
