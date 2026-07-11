<script setup>
import { ref } from 'vue'
import { MagicStick, RefreshRight } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import MarkdownIt from 'markdown-it'
import { requestTraceAiSummary } from './traceAi'

const props = defineProps({
    taskId: { type: Number, required: true },
})

// Markdown 渲染器
const md = new MarkdownIt({
    html: false,
    breaks: true,
    linkify: true,
})

const visible = ref(false)
const currentTraceArtifact = ref(null)
const traceAiAnalyzing = ref(false)
const traceAiResult = ref('')
const traceAiRenderedHtml = ref('')
const traceAiTokenUsage = ref(0)
const traceAiCached = ref(false)

const applyTraceAiResult = (artifact, data) => {
    if (artifact) {
        artifact.ai_summary = data.analysis_result
        artifact.ai_summary_cached = data.cached || false
    }
    traceAiResult.value = data.analysis_result
    traceAiRenderedHtml.value = md.render(data.analysis_result)
    traceAiTokenUsage.value = data.token_usage || 0
    traceAiCached.value = data.cached || false
}

const open = async (artifact, options = {}) => {
    if (!artifact) return
    currentTraceArtifact.value = artifact
    visible.value = true
    traceAiResult.value = ''
    traceAiRenderedHtml.value = ''
    traceAiTokenUsage.value = 0
    traceAiCached.value = false

    traceAiAnalyzing.value = true
    try {
        const data = await requestTraceAiSummary(props.taskId, artifact, options)
        if (data) {
            applyTraceAiResult(artifact, data)
        }
    } catch (err) {
        const msg = err.response?.data?.detail || err.message || 'AI 总结生成失败'
        ElMessage.error(msg)
    } finally {
        traceAiAnalyzing.value = false
    }
}

const reAnalyzeTrace = async () => {
    if (!currentTraceArtifact.value) return
    currentTraceArtifact.value.ai_summary = ''
    currentTraceArtifact.value.ai_summary_cached = false
    await open(currentTraceArtifact.value, { forceRefresh: true })
}

defineExpose({ open })
</script>

<template>
    <el-dialog
        v-model="visible"
        title="Perfetto Trace AI 总结"
        width="70%"
        top="8vh"
        destroy-on-close
    >
        <div class="ai-action-area" v-if="!traceAiResult && !traceAiAnalyzing">
            <el-button
                type="primary"
                :icon="MagicStick"
                size="large"
                round
                @click="open(currentTraceArtifact)"
            >
                ✨ 生成 AI 总结
            </el-button>
            <p class="ai-hint">AI 只会基于当前 Trace 片段生成结论，不代表整段录制的整体体验。</p>
        </div>
        <div class="ai-action-area" v-else-if="traceAiAnalyzing">
            <el-button
                type="primary"
                :icon="MagicStick"
                :loading="true"
                :loading-text="'正在分析中...'"
                size="large"
                round
            >
                ✨ 正在生成 AI 总结
            </el-button>
            <p class="ai-hint analyzing">正在调用模型，请稍候...</p>
        </div>
        <el-card v-else class="ai-analysis-card" shadow="hover">
            <template #header>
                <div class="ai-card-header">
                    <span class="ai-card-title">🤖 AI 卡顿诊断</span>
                    <div class="ai-card-actions">
                        <el-tag v-if="traceAiCached" type="info" size="small" effect="plain">缓存结果</el-tag>
                        <el-tag v-if="traceAiTokenUsage > 0" type="warning" size="small" effect="plain">Token: {{ traceAiTokenUsage }}</el-tag>
                        <el-button
                            :icon="RefreshRight"
                            size="small"
                            text
                            type="primary"
                            @click="reAnalyzeTrace"
                        >
                            重新分析
                        </el-button>
                    </div>
                </div>
            </template>
            <div class="ai-markdown-body" v-html="traceAiRenderedHtml"></div>
        </el-card>
    </el-dialog>
</template>

<style scoped src="./aiAnalysis.css"></style>
