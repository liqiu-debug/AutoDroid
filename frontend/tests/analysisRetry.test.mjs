import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { describeError } from '../src/utils/errors.js'
import {
    TRANSIENT_RETRY_DELAYS_MS,
    isTransientError,
    requestWithTransientRetry,
} from '../src/utils/retryTransient.js'

// 分析类请求的传输级重试契约：
// - **只重试「响应路径断了」**：连接直接断掉，或拿回网关错误页（纯文本/HTML）。
//   服务端用 JSON 答复的都是它真的答了（含应用层 5xx：AI 服务挂了、正文为空、LLM 超时），
//   重试只会把 80s 超时和 token 计费放大三倍。光看状态码不行——网关给 504，
//   应用层「AI 服务响应超时」也是 504，得看 content-type；
// - 取消、会话过期这类本地拒绝是终态，不重试；
// - 重试时把 force_refresh 降成 false：首次多半已算完，别为一次丢失的响应再烧一遍 token；
// - 首次请求的载荷必须原样传，不能被「降级」逻辑污染；
// - 重试次数由 delays 数组长度决定，且必须有界（非数组也不能造成无限循环）。
const OK = { data: { success: true } }

function fail(status, contentType = 'application/json', body = { detail: 'x' }) {
    const error = new Error(`HTTP ${status ?? 'reset'}`)
    if (status !== undefined) {
        error.response = { status, data: body, headers: { 'content-type': contentType } }
    }
    return error
}

/** Cloudflare/网关的错误页：状态码 502/504，body 是纯文本。 */
const gatewayError = () => fail(504, 'text/plain; charset=UTF-8', 'error code: 502')
/** 应用层 5xx：服务端真的答了，body 是 JSON。 */
const appError = (status = 502) => fail(status, 'application/json', { detail: 'AI 服务未返回可分析的正文' })

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

// ==================== 什么才算传输级失败 ====================

test('网关错误页算传输级失败（响应本身没送到）', () => {
    assert.equal(isTransientError(gatewayError()), true)
    assert.equal(isTransientError(fail(undefined)), true, '连接直接断掉也算')
})

test('服务端用 JSON 答复的一律不重试，包括应用层 5xx', () => {
    // 应用层 502「正文为空」/ 504「AI 服务响应超时」/ 500 兜底 —— 都是确定性失败
    for (const status of [500, 502, 504]) {
        assert.equal(isTransientError(appError(status)), false, `应用层 ${status} 不能重试`)
    }
    assert.equal(isTransientError(fail(400)), false)
})

test('取消与会话过期是终态，不重试', () => {
    const canceled = new Error('canceled')
    canceled.name = 'CanceledError'
    assert.equal(isTransientError(canceled), false)

    const canceledByCode = new Error('canceled')
    canceledByCode.code = 'ERR_CANCELED'
    assert.equal(isTransientError(canceledByCode), false)

    const sessionExpired = new Error('登录状态已失效')
    sessionExpired.code = 'ERR_SESSION_EXPIRED'
    assert.equal(isTransientError(sessionExpired), false)
})

// ==================== 重试行为 ====================

test('首次成功时不重试，载荷原样传给请求', async () => {
    const request = record()
    request.script = [OK]

    const payload = { log_text: 'x', force_refresh: true }
    const result = await requestWithTransientRetry(request, payload, { delays: [0, 0] })

    assert.equal(result, OK)
    assert.equal(request.calls.length, 1)
    assert.deepEqual(request.calls[0], payload)
})

test('网关 504 后重试，且重试把 force_refresh 降成 false', async () => {
    const request = record()
    request.script = [gatewayError(), OK]

    await requestWithTransientRetry(request, { log_text: 'x', force_refresh: true }, { delays: [0, 0] })

    assert.equal(request.calls.length, 2)
    assert.equal(request.calls[0].force_refresh, true, '首次请求不能被降级')
    assert.equal(request.calls[1].force_refresh, false, '重试要能命中服务端缓存')
    assert.equal(request.calls[1].log_text, 'x', '其余载荷不变')
})

test('应用层 5xx 不重试 —— 这是本次改动最要紧的一条', async () => {
    const request = record()
    const bad = appError(504)
    request.script = [bad, OK, OK]

    await assert.rejects(
        () => requestWithTransientRetry(request, { force_refresh: true }, { delays: [0, 0] }),
        (error) => error === bad,
    )
    assert.equal(request.calls.length, 1, 'AI 服务故障时不能打三次、等三段超时')
})

test('4xx 与取消都不触发重试', async () => {
    for (const error of [fail(400), fail(422), (() => { const e = new Error('x'); e.name = 'CanceledError'; return e })()]) {
        const request = record()
        request.script = [error]
        await assert.rejects(() => requestWithTransientRetry(request, {}, { delays: [0, 0] }), (e) => e === error)
        assert.equal(request.calls.length, 1)
    }
})

test('重试用尽后抛最后一次的错，请求次数 = 1 + delays 长度', async () => {
    const request = record()
    const last = gatewayError()
    request.script = [gatewayError(), gatewayError(), last]

    await assert.rejects(
        () => requestWithTransientRetry(request, {}, { delays: [0, 0] }),
        (error) => error === last,
    )
    assert.equal(request.calls.length, 3)
})

test('重试次数由 delays 数组长度决定（delays:[] 就不重试）', async () => {
    for (const [delays, expected] of [[[], 1], [[0], 2], [[0, 0, 0], 4]]) {
        const request = record()
        request.script = Array.from({ length: expected }, () => gatewayError())
        await assert.rejects(() => requestWithTransientRetry(request, {}, { delays }))
        assert.equal(request.calls.length, expected, `delays=${JSON.stringify(delays)}`)
    }
})

test('delays 不是数组时退回默认节奏，不会无限重试', async () => {
    // 用假 setTimeout 数清调用次数，不然默认节奏的 1.5s+3s 会把这条测试拖慢
    const realSetTimeout = globalThis.setTimeout
    globalThis.setTimeout = (fn) => { fn(); return 0 }
    try {
        const request = record()
        request.script = Array.from({ length: 10 }, () => gatewayError())
        await assert.rejects(() => requestWithTransientRetry(request, {}, { delays: 1500 }))
        assert.equal(request.calls.length, 1 + TRANSIENT_RETRY_DELAYS_MS.length)
    } finally {
        globalThis.setTimeout = realSetTimeout
    }
})

test('两次重试之间真的会等，不是连着打', async () => {
    const request = record()
    request.script = [gatewayError(), gatewayError(), OK]
    const started = Date.now()

    await requestWithTransientRetry(request, {}, { delays: [60, 60] })

    assert.ok(Date.now() - started >= 110, '两段退避加起来至少 110ms')
})

test('weakenForceRefresh=false 时重试保留原来的 force_refresh', async () => {
    const request = record()
    request.script = [gatewayError(), OK]

    await requestWithTransientRetry(
        request,
        { force_refresh: true },
        { delays: [0, 0], weakenForceRefresh: false },
    )

    assert.equal(request.calls[1].force_refresh, true)
})

test('默认重试节奏是「先快后慢」', () => {
    assert.deepEqual(TRANSIENT_RETRY_DELAYS_MS, [1500, 3000])
})

// ==================== 重试用尽后给用户看的文案 ====================

test('网关错误页的英文 body 不外露，落到中文兜底', () => {
    // 此前 describeError 把 "error code: 502" 当可读正文，跳过 STATUS_TEXT，
    // 用户看到的就是 Cloudflare 的英文串
    assert.equal(describeError(gatewayError()), '服务响应超时，请稍后重试')
    assert.equal(describeError(fail(502, 'text/html', '<!doctype html><html>')), '服务暂时不可用，请稍后重试')
    // 应用层自己的中文 detail 要照常显示
    assert.equal(describeError(appError(502)), 'AI 服务未返回可分析的正文')
})

// ==================== 两处调用点确实接上了 ====================

test('崩溃日志分析与 Trace AI 总结都走传输级重试', () => {
    const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
    const dialog = read('../src/views/fastbot/components/LogAnalysisDialog.vue')
    assert.match(dialog, /requestWithTransientRetry\(/)
    assert.match(dialog, /@\/utils\/retryTransient/)

    const trace = read('../src/views/fastbot/components/traceAi.js')
    assert.match(trace, /requestWithTransientRetry\(/)
})
