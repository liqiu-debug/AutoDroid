<script setup>
import { computed, ref } from 'vue'
import FastbotReplayPlayer from '@/components/FastbotReplayPlayer.vue'
import { getReplayFilename } from './reportFormatters'

defineProps({
    taskId: { type: Number, required: true },
})

const visible = ref(false)
const currentReplayEvent = ref(null)

const currentReplayTitle = computed(() => {
    const event = currentReplayEvent.value
    if (!event) return '本地复现回放'
    return `${event.type} 本地复现回放 · ${event.time || '--'}`
})

const open = (event) => {
    currentReplayEvent.value = event
    visible.value = true
}

defineExpose({ open })
</script>

<template>
    <el-dialog
        v-model="visible"
        :title="currentReplayTitle"
        width="70%"
        top="8vh"
        destroy-on-close
    >
        <FastbotReplayPlayer
            v-if="visible && currentReplayEvent"
            :key="`${currentReplayEvent.time}-${getReplayFilename(currentReplayEvent)}`"
            :task-id="taskId"
            :filename="getReplayFilename(currentReplayEvent)"
        />
    </el-dialog>
</template>
