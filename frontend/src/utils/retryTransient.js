/**
 * 分析类请求的「传输级失败」重试。
 *
 * 为什么这类请求特别需要：服务端按清洗后日志的 md5 缓存分析结果，而失败是断在
 * 「origin → Cloudflare」的回程上——实测 origin 早已算完并写好缓存，只是响应回不来，
 * 用户看到的是 Cloudflare 的 `error code: 502`。紧接其后的重试 2.9 秒就命中缓存返回 200。
 * 所以重试成本极低（多数时候是缓存命中），却能把约 40% 的偶发 502 全部吃掉。
 *
 * 重试时把 force_refresh 降成 false：万一第一次真算完了，别为一次丢失的响应再烧一遍 token。
 * 4xx 不重试——那是请求本身的问题，重试只会把同样的错再打一遍。
 */

export const TRANSIENT_RETRY_DELAYS_MS = [1500, 3000]

/** 传输/网关层失败才重试：没有 response（连接就断了）或 5xx。 */
export function isTransientError(error) {
    const status = error?.response?.status
    return !status || status >= 500
}

/**
 * @param {Function} request  (payload) => Promise，失败抛错
 * @param {object}   payload  首次请求用的载荷
 * @param {object}   [options] delays: 重试间隔数组；weakenForceRefresh: 重试时把 force_refresh 置 false
 */
export async function requestWithTransientRetry(request, payload, options = {}) {
    const delays = options.delays ?? TRANSIENT_RETRY_DELAYS_MS
    const weakenForceRefresh = options.weakenForceRefresh !== false

    for (let attempt = 0; ; attempt++) {
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
}
