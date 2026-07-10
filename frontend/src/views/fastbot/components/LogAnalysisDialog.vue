<script setup>
import { ref } from 'vue'
import { MagicStick, RefreshRight } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import MarkdownIt from 'markdown-it'
import api from '@/api'

const props = defineProps({
    packageName: { type: String, default: '' },
    deviceSerial: { type: String, default: '' },
})

// Markdown 渲染器
const md = new MarkdownIt({
    html: false,
    breaks: true,
    linkify: true,
})

const visible = ref(false)
const logContent = ref('')
const logEventType = ref('')

// AI 分析状态
const aiAnalyzing = ref(false)
const aiResult = ref('')
const aiRenderedHtml = ref('')
const aiTokenUsage = ref(0)
const aiCached = ref(false)
const showAiResult = ref(false)

const open = (event) => {
    logEventType.value = event.type
    logContent.value = event.full_log || '无日志数据'
    visible.value = true
    // 重置 AI 分析状态
    aiResult.value = ''
    aiRenderedHtml.value = ''
    showAiResult.value = false
    aiTokenUsage.value = 0
    aiCached.value = false
}

// AI 智能分析
const analyzeLog = async (options = {}) => {
    if (!logContent.value || logContent.value === '无日志数据') {
        ElMessage.warning('没有可分析的日志内容')
        return
    }

    const forceRefresh = Boolean(options.forceRefresh)
    aiAnalyzing.value = true
    try {
        const res = await api.analyzeLog({
            log_text: logContent.value,
            package_name: props.packageName || '',
            device_info: props.deviceSerial || '',
            force_refresh: forceRefresh,
        })
        const data = res.data
        if (data.success) {
            aiResult.value = data.analysis_result
            aiRenderedHtml.value = md.render(data.analysis_result)
            aiTokenUsage.value = data.token_usage || 0
            aiCached.value = data.cached || false
            showAiResult.value = true
        } else {
            ElMessage.error('分析失败，请重试')
        }
    } catch (err) {
        const msg = err.response?.data?.detail || err.message || '分析请求失败'
        ElMessage.error(msg)
    } finally {
        aiAnalyzing.value = false
    }
}

// 重新分析
const reAnalyze = () => {
    showAiResult.value = false
    aiResult.value = ''
    aiRenderedHtml.value = ''
    aiCached.value = false
    analyzeLog({ forceRefresh: true })
}

defineExpose({ open })
</script>

<template>
    <!-- 日志查看弹窗 (含 AI 分析) -->
    <el-dialog
        v-model="visible"
        :title="`${logEventType} 日志快照`"
        width="80%"
        top="5vh"
        destroy-on-close
    >
        <!-- 原始日志 -->
        <pre class="log-viewer">{{ logContent }}</pre>

        <!-- AI 分析区域 -->
        <el-divider content-position="center">
            <span style="color: #909399; font-size: 12px;">AI 智能分析</span>
        </el-divider>

        <!-- 分析按钮 (未分析时显示) -->
        <div class="ai-action-area" v-if="!showAiResult">
            <el-button
                type="primary"
                :icon="MagicStick"
                :loading="aiAnalyzing"
                :loading-text="'正在分析中...'"
                size="large"
                round
                @click="analyzeLog"
            >
                ✨ AI 智能根因分析
            </el-button>
            <p class="ai-hint" v-if="!aiAnalyzing">点击按钮，AI 将自动提取关键日志并给出根因分析与修复建议</p>
            <p class="ai-hint analyzing" v-else>正在清洗日志并调用 AI 模型，请稍候...</p>
        </div>

        <!-- 分析结果卡片 -->
        <el-card v-if="showAiResult" class="ai-analysis-card" shadow="hover">
            <template #header>
                <div class="ai-card-header">
                    <span class="ai-card-title">🤖 AI 诊断报告</span>
                    <div class="ai-card-actions">
                        <el-tag v-if="aiCached" type="info" size="small" effect="plain">缓存结果</el-tag>
                        <el-tag v-if="aiTokenUsage > 0" type="warning" size="small" effect="plain">Token: {{ aiTokenUsage }}</el-tag>
                        <el-button
                            :icon="RefreshRight"
                            size="small"
                            text
                            type="primary"
                            @click="reAnalyze"
                        >
                            重新分析
                        </el-button>
                    </div>
                </div>
            </template>
            <div class="ai-markdown-body" v-html="aiRenderedHtml"></div>
        </el-card>
    </el-dialog>
</template>

<style scoped src="./aiAnalysis.css"></style>

<style scoped>
/* ==================== 日志查看器 ==================== */
.log-viewer {
    background: #1e1e1e;
    color: #d4d4d4;
    font-family: 'Menlo', 'Monaco', 'Courier New', monospace;
    font-size: 12px;
    line-height: 1.5;
    padding: 16px;
    border-radius: 6px;
    max-height: 50vh;
    overflow: auto;
    white-space: pre-wrap;
    word-break: break-all;
    margin: 0;
}
</style>
