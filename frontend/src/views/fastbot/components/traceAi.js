import { ElMessage } from 'element-plus'
import api from '@/api'
import { requestWithTransientRetry } from '@/utils/retryTransient'

/**
 * 获取 Perfetto Trace 的 AI 总结。
 * - Trace 未完成结构化分析时提示并返回 null
 * - 命中 artifact 本地缓存时直接返回，不再请求后端
 */
export const requestTraceAiSummary = async (taskId, artifact, options = {}) => {
    if (!artifact) return null
    if (artifact.analysis_status !== 'ANALYZED') {
        ElMessage.warning('当前 Trace 还没有可用的结构化分析结果')
        return null
    }
    const forceRefresh = Boolean(options.forceRefresh)

    if (!forceRefresh && artifact.ai_summary) {
        return {
            analysis_result: artifact.ai_summary,
            token_usage: 0,
            cached: artifact.ai_summary_cached !== false,
        }
    }

    // 传输级重试同 analyzeLog：服务端也缓存 trace 总结，502 多半是回程被掐断，
    // 重试直接命中缓存；重试时把 force_refresh 降成 false，别为丢失的响应再烧一遍 token
    const res = await requestWithTransientRetry(
        (payload) => api.analyzeFastbotTrace(taskId, payload),
        {
            trace_path: artifact.path,
            force_refresh: forceRefresh,
        },
    )
    const data = res.data
    if (!data.success) {
        throw new Error('AI 总结生成失败')
    }
    if (forceRefresh && data.cached) {
        // 点了「重新生成」却拿到缓存结果 = 首次响应丢了、重试降级过，得说出来
        ElMessage.info('重新生成未成功，已返回缓存结果')
    }
    return data
}
