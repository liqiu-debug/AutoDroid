import { ElMessage } from 'element-plus'
import api from '@/api'

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

    const res = await api.analyzeFastbotTrace(taskId, {
        trace_path: artifact.path,
        force_refresh: forceRefresh,
    })
    const data = res.data
    if (!data.success) {
        throw new Error('AI 总结生成失败')
    }
    return data
}
