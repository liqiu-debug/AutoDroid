<script setup>
import { ref } from 'vue'
import { imageMatchStatusText } from '@/utils/imageOptions'

const props = defineProps({ screenshot: { type: String, default: '' }, result: { type: Object, default: null } })
const dimensions = ref([1, 1])
const onLoad = event => { dimensions.value = [event.target.naturalWidth, event.target.naturalHeight] }
const rectStyle = rect => ({
  left: `${100 * rect[0] / dimensions.value[0]}%`, top: `${100 * rect[1] / dimensions.value[1]}%`,
  width: `${100 * rect[2] / dimensions.value[0]}%`, height: `${100 * rect[3] / dimensions.value[1]}%`
})
const pointStyle = point => ({ left: `${100 * point[0] / dimensions.value[0]}%`, top: `${100 * point[1] / dimensions.value[1]}%` })
</script>

<template>
  <div class="image-preview-message" role="status">{{ imageMatchStatusText(props.result) }}</div>
  <div v-if="props.screenshot" class="image-preview-frame">
    <img :src="props.screenshot" alt="当前截图的图像定位验证" @load="onLoad" />
    <template v-for="(candidate, index) in props.result?.candidates || []" :key="index">
      <div v-if="Array.isArray(candidate.rect)" class="image-preview-candidate" :class="{ accepted: candidate.accepted }" :style="rectStyle(candidate.rect)">
        <span>{{ index + 1 }} · {{ Number(candidate.similarity || 0).toFixed(3) }}</span>
      </div>
      <div v-if="Array.isArray(candidate.point)" class="image-preview-point" :style="pointStyle(candidate.point)"></div>
    </template>
    <div v-if="Array.isArray(props.result?.point)" class="image-preview-point" :style="pointStyle(props.result.point)"></div>
  </div>
  <p class="image-preview-help">绿色框表示通过校验的候选，红点表示拟点击位置。此次验证只定位。</p>
</template>

<style scoped>
.image-preview-message { margin-bottom: 12px; line-height: 1.6; }
.image-preview-frame { position: relative; width: fit-content; max-width: 100%; margin: auto; line-height: 0; }
.image-preview-frame img { display: block; max-width: 100%; max-height: 60vh; object-fit: contain; }
.image-preview-candidate { position: absolute; border: 2px solid #e6a23c; box-sizing: border-box; pointer-events: none; }
.image-preview-candidate.accepted { border-color: #17a36b; }
.image-preview-candidate span { position: absolute; bottom: 100%; left: -2px; background: #fff; color: #222; font-size: 11px; line-height: 16px; padding: 1px 4px; white-space: nowrap; }
.image-preview-point { position: absolute; width: 12px; height: 12px; background: #f04438; border: 2px solid white; border-radius: 50%; transform: translate(-50%, -50%); box-shadow: 0 0 0 1px #f04438; pointer-events: none; }
.image-preview-help { color: var(--ad-muted); font-size: 12px; line-height: 1.5; }
</style>
