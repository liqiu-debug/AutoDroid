import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import {
    formatStartupMs, startupDeviceReason, startupCounts, startupPercentileText,
    startupMetricStats, startupVerdicts, startupCanViewReport, startupTraceAvailable,
    startupReadyLocators, startupMetrics, createStartupSubmission, startupProgressText,
    mergeStartupTasks, startupSubmissionForSession,
    startupStateText, startupMetricMetadata, startupMetricSourceText, startupTraceEvidence,
} from '../src/utils/startup.js'
import { formatMs, formatReadyStatus, formatStartupMode, formatTraceAnalysisStatus, getPrimaryTraceCause, getTraceCaptureModeLabel } from '../src/views/fastbot/components/reportFormatters.js'

const device = (overrides = {}) => ({ platform: 'android', available: true, executor_kind: 'desktop', capabilities: {
    protocol_version: 1, platforms: ['android'], metrics: ['total_time_ms', 'automation_ms', 'ready_ms'],
    ready_locators: ['text', 'xpath'], trace: ['perfetto'], web_dispatch: true,
}, ...overrides })

test('missing timings never become zero and a real zero is kept in every report formatter', () => {
    for (const formatter of [formatMs, formatStartupMs]) {
        for (const missing of [null, undefined, '', false, NaN, Infinity]) assert.equal(formatter(missing), '未采集')
        assert.equal(formatter(0), '0 ms')
        assert.equal(formatter('0'), '0 ms')
    }
})

test('startup devices fail closed for old protocol, ownership dispatch and unavailable reasons', () => {
    assert.equal(startupDeviceReason(device()), '')
    assert.match(startupDeviceReason(device({ capabilities: {} })), /升级客户端/)
    assert.match(startupDeviceReason(device({ capabilities: { protocol_version: 1, platforms: ['ios'] } })), /不支持此平台/)
    assert.equal(startupDeviceReason(device({ available: false, unavailable_reason: 'WDA 未就绪' })), 'WDA 未就绪')
    const localOnly = device()
    localOnly.capabilities.web_dispatch = false
    assert.equal(startupDeviceReason(localOnly), '')
    assert.match(startupDeviceReason(localOnly, { web: true }), /不支持 Web 派发/)
})

test('multi-device options expose only shared locator, metric and trace capabilities', () => {
    const a = device(), b = device()
    b.capabilities.ready_locators = ['xpath']
    b.capabilities.metrics = ['automation_ms']
    b.capabilities.trace = false
    assert.deepEqual(startupReadyLocators([a, b]), ['xpath'])
    assert.deepEqual(startupMetrics([a, b]), ['automation_ms'])
    assert.equal(startupTraceAvailable([a, b]), false)
    assert.equal(startupTraceAvailable([]), false)
    assert.equal(startupTraceAvailable([a]), true)
})

test('v2 counts keep requested/completed/invalid distinct and exclude diagnostic reruns', () => {
    const summary = { measurement_version: 'startup-v2', startup_config: { startup_modes: ['cold', 'hot'], iterations: 3, ready_check: { enabled: true } }, startup_runs: [
        { sample_valid: true, launch_success: true, success: true, ready_status: 'FOUND', performance_passed: true },
        { sample_valid: false, launch_success: true, success: false, ready_status: 'TIMEOUT', performance_passed: null },
        { sample_valid: true, launch_success: false, success: false, ready_status: 'ERROR', performance_passed: false },
        { diagnostic: true, sample_valid: true, launch_success: true, success: true, ready_status: 'FOUND', performance_passed: true },
    ] }
    assert.deepEqual(startupCounts(summary), { requested: 6, completed: 3, valid: 2, invalid: 1, failed: 2 })
    assert.deepEqual(startupVerdicts(summary), { launch: '2/3', ready: '1/3', performance: '1/2' })
    assert.equal(startupProgressText({ status: 'RUNNING', summary }), '3/6')
    assert.equal(startupCanViewReport({ status: 'ABORTED', summary }), true)
    assert.equal(startupCanViewReport({ status: 'QUEUED' }), false)
    assert.equal(startupCanViewReport({ status: 'RUNNING', summary }), true)
})

test('v2 metric stats never fall back to aliases and small samples label percentiles', () => {
    const summary = { measurement_version: 'startup-v2', startup_aggregate: { cold: { p90_ms: 999, metrics: {
        automation_ms: { count: 3, p90_ms: 0, samples_sufficient: false },
        ready_ms: { count: 20, p90_ms: 850, samples_sufficient: true },
    } } } }
    assert.equal(startupMetricStats(summary, 'cold', 'total_time_ms'), null)
    assert.equal(startupPercentileText(summary, 'cold', 'automation_ms'), '0 ms · 样本不足')
    assert.equal(startupPercentileText(summary, 'cold', 'ready_ms'), '850 ms')
    assert.equal(startupPercentileText(summary, 'hot', 'ready_ms'), '未采集')
})

test('legacy report values remain readable without inventing validity or automation timings', () => {
    const summary = { startup_config: { startup_modes: ['cold'], iterations: 3 }, success_count: 2, fail_count: 1, startup_aggregate: { cold: { p90_ms: 100, ready_p90_ms: 0 } } }
    assert.equal(startupPercentileText(summary, 'cold', 'total_time_ms'), '100 ms')
    assert.equal(startupPercentileText(summary, 'cold', 'ready_ms'), '0 ms')
    assert.equal(startupMetricStats(summary, 'cold', 'automation_ms'), null)
    assert.deepEqual(startupCounts(summary), { requested: 3, completed: 3, valid: null, invalid: null, failed: 1 })
})

test('submission identifiers survive transport errors, server errors and component payload mutation', () => {
    let sequence = 0
    const submission = createStartupSubmission(() => `r${++sequence}`)
    const payload = { platform: 'ios', device_serials: ['A'], thresholds: { metric: 'automation_ms', cold_ms: 1000 } }
    const first = submission.prepare(payload)
    first.device_serials.push('MUTATED')
    assert.equal(submission.reject(new Error('timeout')), true)
    const retry = submission.prepare({ platform: 'android' })
    assert.deepEqual(retry.device_serials, ['A'])
    assert.equal(retry.request_id, 'r1')
    assert.equal(submission.reject({ response: { status: 503 } }), true)
    submission.resolve()
    assert.equal(submission.prepare(payload).request_id, 'r2')
    assert.equal(submission.reject({ response: { status: 422 } }), false)
    assert.equal(submission.prepare(payload).request_id, 'r3')
})

test('unconfirmed submissions survive page recreation within one login session', () => {
    const first = startupSubmissionForSession('navigation-test')
    const payload = first.prepare({ device_serials: ['A'] })
    first.reject(new Error('timeout'))
    const restored = startupSubmissionForSession('navigation-test')
    assert.equal(restored.peek().request_id, payload.request_id)
    assert.equal(startupSubmissionForSession('another-account-session').hasPending(), false)
    restored.resolve()
})

test('pending local reports keep their terminal state while cloud sync is delayed', () => {
    const remote = [{ id: 8, status: 'RUNNING', package_name: 'a.b' }]
    const local = [{ id: 8, status: 'COMPLETED', sync_state: 'pending', report: { summary: { completed_count: 3 } } }]
    const merged = mergeStartupTasks(remote, local)
    assert.equal(merged.length, 1)
    assert.equal(merged[0].status, 'COMPLETED')
    assert.equal(merged[0].summary.completed_count, 3)
    assert.equal(merged[0].package_name, 'a.b')
    assert.equal(mergeStartupTasks(remote, [{ ...local[0], sync_state: 'synced' }])[0].status, 'RUNNING')
})

test('startup report separates timing boundaries and raw state evidence', () => {
    const source = readFileSync(new URL('../src/views/fastbot/components/StartupReportSection.vue', import.meta.url), 'utf8')
    assert.match(source, /业务就绪成功/)
    assert.match(source, /性能达标 \/ 已评估/)
    assert.match(source, /row\.sample_valid/)
    assert.match(source, /row\.state_before/)
    assert.match(source, /row\.automation_ms/)
    assert.match(source, /历史 Android 启动报告/)
    assert.doesNotMatch(source, /row\.ready_ms \? /)
})

test('startup report translates unverified modes and partial measurement states', () => {
    assert.equal(formatStartupMode('unknown'), '未验证')
    assert.match(formatStartupMode('warm'), /Activity 重建/)
    assert.equal(formatReadyStatus('NOT_CHECKED'), '未检查')
    assert.equal(formatTraceAnalysisStatus('PARTIAL'), '部分分析')
    assert.equal(formatTraceAnalysisStatus('PENDING'), '等待分析')
    assert.equal(getTraceCaptureModeLabel({ capture_mode: 'original_sample' }), '原轮次取证')
})

test('startup trace evidence converts only nanosecond fields and preserves bounded scheduler metrics', () => {
    const artifact = { analysis_status: 'PARTIAL', analysis: {
        analysis_level: 'startup_evidence', window_source: 'android.startup.startups',
        window: { start_ns: 1000000000, end_ns: 1145792410 }, selected_startup_id: 0,
        startup_intervals: [{ startup_id: 0, ts: 1000000000, dur: 145792410, package: 'a.b', startup_type: 'cold', private_detail: 'omit-me' }],
        main_thread_slices: [{ process_name: 'a.b', tid: 1, name: 'bindApplication', duration_ms: 12.125, occurrences: 2 }],
        cpu_running: [{ process_name: 'a.b', thread_name: 'main', tid: 1, running_ms: 0, sched_slices: 3 }],
        main_thread_states: [{ process_name: 'a.b', tid: 1, state: 'R', duration_ms: 4.5, intervals: 1 }],
        limitations: ['缺少部分调度证据', { hidden: 'omit-me' }], tool: { path: 'omit-me', sha256: 'omit-me' }, token: 'omit-me',
    } }
    const result = startupTraceEvidence(artifact)
    assert.equal(result.status, 'PARTIAL')
    assert.equal(result.window_ms, 145.792)
    assert.equal(result.startup_intervals[0].duration_ms, 145.792)
    assert.equal(result.startup_intervals[0].offset_ms, 0)
    assert.equal(result.startup_intervals[0].selected, true)
    assert.equal(result.main_thread_slices[0].duration_ms, 12.125, 'already-ms metrics must not be divided again')
    assert.equal(result.cpu_running[0].running_ms, 0)
    assert.equal(result.main_thread_states[0].duration_ms, 4.5)
    assert.deepEqual(result.limitations, ['缺少部分调度证据'])
    assert.doesNotMatch(JSON.stringify(result), /omit-me/)
    assert.equal(getPrimaryTraceCause(artifact), '-', 'bounded evidence never invents an AI root cause')
    assert.equal(startupTraceEvidence({ analysis: { analysis_level: 'full' } }), null)
})

test('fallback trace windows remain explicitly limited and missing evidence never becomes zero', () => {
    const result = startupTraceEvidence({ analysis: {
        analysis_level: 'startup_evidence', analysis_status: 'PARTIAL', window_source: 'trace_bounds_fallback',
        window: { start_ns: null, end_ns: null }, startup_intervals: [{ dur: null, ts: null }],
        main_thread_slices: [{ duration_ms: null }], cpu_running: [{ running_ms: false }], main_thread_states: null,
    } })
    assert.match(result.source, /缺少启动事件/)
    assert.equal(result.window_ms, null)
    assert.equal(result.startup_intervals[0].duration_ms, null)
    assert.equal(result.startup_intervals[0].offset_ms, null)
    assert.equal(result.main_thread_slices[0].duration_ms, null)
    assert.equal(result.cpu_running[0].running_ms, null)
    assert.deepEqual(result.main_thread_states, [])
    const source = readFileSync(new URL('../src/views/fastbot/components/StartupReportSection.vue', import.meta.url), 'utf8')
    for (const key of ['startup_intervals', 'main_thread_slices', 'cpu_running', 'main_thread_states']) assert.ok(source.includes(key))
    assert.match(source, /部分分析：证据有限/)
    assert.match(source, /getReportAssetUrl/)
    assert.doesNotMatch(source, /JSON\.stringify/)
})

test('startup metadata and process state display relevant evidence through whitelisted fields', () => {
    assert.equal(startupStateText({ state: 'foreground', process_id: 12, token: 'hidden' }), '前台 · PID 12')
    assert.match(startupStateText({ value: 2, process_id_source: 'preparation_foreground', process_id: 13 }), /后台挂起 · PID 13/)
    assert.equal(startupStateText(null), '未记录')
    const metadata = startupMetricMetadata({ metrics_metadata: {
        automation_ms: { source: 'host.monotonic.wda', unit: 'ms', description: 'WDA 控制耗时', token: 'hidden' },
        token: { label: 'hidden' },
    } }, 'ios')
    assert.equal(metadata.length, 1)
    assert.equal(metadata[0].source, 'host.monotonic.wda')
    assert.equal(metadata[0].unit, 'ms')
    assert.doesNotMatch(JSON.stringify(metadata), /hidden/)
    assert.equal(startupMetricSourceText({ automation_ms: 'host.monotonic.wda', token: 'hidden' }, 'ios'), 'WDA 启动 / 恢复耗时：host.monotonic.wda')
})
