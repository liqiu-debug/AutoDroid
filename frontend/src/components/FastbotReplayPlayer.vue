<script setup>
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import api from '@/api'

const props = defineProps({
    taskId: {
        type: Number,
        required: true,
    },
    filename: {
        type: String,
        default: '',
    },
})

const videoRef = ref(null)
const videoUrl = ref('')
const loading = ref(false)
const error = ref('')

const revokeVideoUrl = () => {
    if (!videoUrl.value) return
    window.URL.revokeObjectURL(videoUrl.value)
    videoUrl.value = ''
}

const resetPlayer = () => {
    if (videoRef.value) {
        videoRef.value.pause?.()
        videoRef.value.removeAttribute('src')
        videoRef.value.load?.()
    }
    revokeVideoUrl()
}

const handleVideoError = () => {
    if (!error.value) {
        error.value = '回放播放失败，请刷新报告后重试'
    }
}

const normalizeReplayBlob = (blob) => {
    if (!(blob instanceof Blob)) return null
    if (blob.type === 'video/mp4') return blob
    return new Blob([blob], { type: 'video/mp4' })
}

const attachVideoAndPlay = async (blob) => {
    const replayBlob = normalizeReplayBlob(blob)
    if (!replayBlob || replayBlob.size <= 0) {
        throw new Error('回放文件为空')
    }

    videoUrl.value = window.URL.createObjectURL(replayBlob)
    await nextTick()
    if (!videoRef.value) {
        throw new Error('播放器初始化失败')
    }
    videoRef.value.load?.()
    const playPromise = videoRef.value.play?.()
    if (playPromise && typeof playPromise.catch === 'function') {
        playPromise.catch(() => {})
    }
}

const loadReplay = async () => {
    resetPlayer()
    if (!props.filename) {
        error.value = '回放文件不存在'
        return
    }

    loading.value = true
    error.value = ''

    try {
        const res = await api.getFastbotReplay(props.taskId, props.filename)
        await attachVideoAndPlay(res?.data)
    } catch (err) {
        resetPlayer()
        error.value = err?.response?.data?.detail || err?.message || '回放加载失败'
    } finally {
        loading.value = false
    }
}

onMounted(() => {
    loadReplay()
})

watch(
    () => [props.taskId, props.filename],
    () => {
        loadReplay()
    },
)

onUnmounted(() => {
    resetPlayer()
})
</script>

<template>
    <div class="fastbot-replay-player" v-loading="loading">
        <video
            ref="videoRef"
            class="replay-video"
            :src="videoUrl"
            controls
            autoplay
            muted
            playsinline
            preload="metadata"
            @error="handleVideoError"
        />
        <el-empty v-if="!loading && error" :description="error" />
        <div v-else class="replay-hint">
            回放内容为后端转封装的 MP4，覆盖 Crash / ANR 前 30 秒和事件后补录片段。
        </div>
    </div>
</template>

<style scoped>
.fastbot-replay-player {
    display: flex;
    flex-direction: column;
    gap: 12px;
}

.replay-video {
    width: 100%;
    max-height: 68vh;
    border-radius: 8px;
    background: #000;
    outline: none;
}

.replay-hint {
    font-size: 12px;
    color: #909399;
    text-align: center;
}
</style>
