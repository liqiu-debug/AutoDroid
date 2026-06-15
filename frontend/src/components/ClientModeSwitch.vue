<script setup>
import { computed } from 'vue'
import { Cellphone, Monitor, Operation } from '@element-plus/icons-vue'
import { useClientMode } from '@/composables/useClientMode'

const props = defineProps({
  compact: {
    type: Boolean,
    default: false,
  },
  light: {
    type: Boolean,
    default: false,
  },
})

const { mode, effectiveMode, setMode } = useClientMode()

const modeItems = [
  { value: 'auto', label: '自动', icon: Operation },
  { value: 'pc', label: 'PC', icon: Monitor },
  { value: 'mobile', label: '移动', icon: Cellphone },
]

const activeItem = computed(() => modeItems.find(item => item.value === mode.value) || modeItems[0])
const activeIcon = computed(() => activeItem.value.icon)
const activeText = computed(() => {
  if (mode.value === 'auto') {
    return effectiveMode.value === 'mobile' ? '自动·移动' : '自动·PC'
  }
  return activeItem.value.label
})
</script>

<template>
  <el-dropdown trigger="click" @command="setMode">
    <el-button
      class="client-mode-button"
      :class="{ 'is-light': props.light }"
      size="small"
      plain
    >
      <el-icon><component :is="activeIcon" /></el-icon>
      <span v-if="!props.compact">{{ activeText }}</span>
    </el-button>
    <template #dropdown>
      <el-dropdown-menu>
        <el-dropdown-item
          v-for="item in modeItems"
          :key="item.value"
          :command="item.value"
        >
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
        </el-dropdown-item>
      </el-dropdown-menu>
    </template>
  </el-dropdown>
</template>

<style scoped>
.client-mode-button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border-radius: 6px;
}

.client-mode-button.is-light {
  --el-button-bg-color: transparent;
  --el-button-border-color: rgba(255, 255, 255, 0.36);
  --el-button-text-color: #f4f4f5;
  --el-button-hover-bg-color: rgba(255, 255, 255, 0.08);
  --el-button-hover-border-color: rgba(255, 255, 255, 0.58);
  --el-button-hover-text-color: #ffffff;
}
</style>
