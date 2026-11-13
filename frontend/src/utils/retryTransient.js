/**
 * 分析类请求的「传输级失败」重试。
 *
 * 为什么这类请求特别需要：服务端按清洗后日志的 md5 缓存分析结果，而线上偶发的失败是
 * 断在「origin → Cloudflare」的回程上——实测 origin 早已算完并写好缓存，只是响应回不来，
 * 用户看到的是 Cloudflare 的纯文本错误页。紧接其后的重试 2.9 秒就命中缓存返回 200。
 * 所以重试成本极低（多数时候是缓存命中），却能把约 40% 的偶发失败吃掉。
 *
 * **只重试「响应路径断了」这一类**。服务端真的答了的 5xx（AI 服务挂了、正文为空、
 * LLM 超时）是确定性失败，重试只会把 80s 超时和 token 计费放大三倍，用户要盯着
 * 「正在分析中」等几分钟。区分办法是 content-type：应用层错误一律是 JSON
 * （{"detail":...}），网关错误页是 text/plain / text/html。光看状态码不行——
 * 网关给 504，应用层的「AI 服务响应超时」也是 504。
 *
 * 重试时把 force_refresh 降成 false：万一第一次真算完了，别为一次丢失的响应再烧一遍 token。
 * 缓存没写上时 force_refresh=false 也只是正常重新分析，不会漏结果。
 */

import { isCanceled } from './errors.js'

export const TRANSIENT_RETRY_DELAYS_MS = [1500, 3000]

/** 请求拦截器本地拒绝（会话过期）没有出网，重试只是把同一个本地错误再打几遍。 */
const isLocalRejection = error => error?.code === 'ERR_SESSION_EXPIRED'

function isJsonResponse(response) {
    const headers = response?.headers
    const type = typeof headers?.get === 'function' ? headers.get('content-type') : headers?.['content-type']
    return /json/i.test(String(type || ''))
}

/**
 * 传输级失败才重试：连接直接断掉（没有任何响应），或者拿回了网关错误页。
 * 服务端用 JSON 答复的（含 4xx 与应用层 5xx）都不是传输问题，不重试。
 */
export function isTransientError(error) {
    if (!error || isCanceled(error) || isLocalRejection(error)) return false
    const response = error.response
    if (!response) return true
    return response.status >= 500 && !isJsonResponse(response)
}

/**
 * @param {Function} request  (payload) => Promise，失败抛错
 * @param {object}   payload  首次请求用的载荷
 * @param {object}   [options] delays: 重试间隔数组；weakenForceRefresh: 重试时把 force_refresh 置 false
 */
export async function requestWithTransientRetry(request, payload, options = {}) {
    // 非数组一律退回默认值：delays.length 一旦是 undefined，下面的有界循环就退不出去了
    const delays = Array.isArray(options.delays) ? options.delays : TRANSIENT_RETRY_DELAYS_MS
    const weakenForceRefresh = options.weakenForceRefresh !== false

    for (let attempt = 0; attempt <= delays.length; attempt++) {
        const body = attempt === 0 || !weakenForceRefresh
            ? payload
            : { ...payload, force_refresh: false }
        try {
            return await request(body)
        } catch (error) {
            if (attempt >= delays.length || !isTransientError(error)) {
                throw error
            }
            await new Promise((resolve) => setTimeout(resolve, delays[attempt]))
        }
    }
    throw new Error('requestWithTransientRetry: unreachable')
}
