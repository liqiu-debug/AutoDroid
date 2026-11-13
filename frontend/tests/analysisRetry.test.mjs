import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
    TRANSIENT_RETRY_DELAYS_MS,
    isTransientError,
    requestWithTransientRetry,
} from '../src/utils/retryTransient.js'

// 分析类请求的传输级重试契约：
// - 5xx / 无 response（连接就断了）才重试，4xx 是请求本身的问题，重试只会把同样的错再打一遍；
// - 重试时把 force_refresh 降成 false —— 服务端按清洗后日志的 md5 缓存结果，
//   首次请求多半已经算完只是响应回不来，重试应命中缓存，别为一次丢失的响应再烧一遍 token；
// - 首次请求的载荷必须原样传，不能被「降级」逻辑污染。
const OK = { data: { success: true } }

function fail(status) {
    const error = new Error(`HTTP ${status ?? 'reset'}`)
    if (status !== undefined) error.response = { status }
    return error
}

function record() {
    const calls = []
    const request = async (payload) => {
        calls.push(payload)
        const step = request.script.shift()
        if (step instanceof Error) throw step
        return step
    }
    request.calls = calls
    request.script = []
    return request
}

test('首次成功时不重试，载荷原样传给请求', async () => {
    const request = record()
    request.script = [OK]

    const payload = { log_text: 'x', force_refresh: true }
    const result = await requestWithTransientRetry(request, payload, { delays: [0, 0] })

    assert.equal(result, OK)
    assert.equal(request.calls.length, 1)
    assert.deepEqual(request.calls[0], payload)
})

test('网关 502 后重试，且重试把 force_refresh 降成 false', async () => {
    const request = record()
    request.script = [fail(502), OK]

    await requestWithTransientRetry(request, { log_text: 'x', force_refresh: true }, { delays: [0, 0] })

    assert.equal(request.calls.length, 2)
    assert.equal(request.calls[0].force_refresh, true, '首次请求不能被降级')
    assert.equal(request.calls[1].force_refresh, false, '重试要能命中服务端缓存')
    assert.equal(request.calls[1].log_text, 'x', '其余载荷不变')
})

test('连接直接断掉（无 response）也按传输级失败重试', async () => {
    const request = record()
    request.script = [fail(undefined), OK]

    await requestWithTransientRetry(request, { force_refresh: true }, { delays: [0, 0] })

    assert.equal(request.calls.length, 2)
    assert.equal(isTransientError(fail(undefined)), true)
})

test('4xx 不重试，直接把错抛出去', async () => {
    const request = record()
    const bad = fail(400)
    request.script = [bad]

    await assert.rejects(
        () => requestWithTransientRetry(request, { force_refresh: true }, { delays: [0, 0] }),
        (error) => error === bad,
    )
    assert.equal(request.calls.length, 1, '4xx 不能触发重试')
})

test('重试用尽后抛最后一次的错，请求次数 = 1 + 重试次数', async () => {
    const request = record()
    const last = fail(503)
    request.script = [fail(502), fail(502), last]

    await assert.rejects(
        () => requestWithTransientRetry(request, {}, { delays: [0, 0] }),
        (error) => error === last,
    )
    assert.equal(request.calls.length, 3)
})

test('weakenForceRefresh=false 时重试保留原来的 force_refresh', async () => {
    const request = record()
    request.script = [fail(502), OK]

    await requestWithTransientRetry(
        request,
        { force_refresh: true },
        { delays: [0, 0], weakenForceRefresh: false },
    )

    assert.equal(request.calls[1].force_refresh, true)
})

test('默认重试节奏是「先快后慢」，不是无脑连打', () => {
    assert.deepEqual(TRANSIENT_RETRY_DELAYS_MS, [1500, 3000])
})

test('两处分析调用都真的走了传输级重试', () => {
    const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
    const dialog = read('../src/views/fastbot/components/LogAnalysisDialog.vue')
    assert.match(dialog, /requestWithTransientRetry\(/, '崩溃日志分析要走重试')
    assert.match(dialog, /@\/utils\/retryTransient/)

    const trace = read('../src/views/fastbot/components/traceAi.js')
    assert.match(trace, /requestWithTransientRetry\(/, 'Trace AI 总结同样暴露在 502 下，也要走重试')
})
