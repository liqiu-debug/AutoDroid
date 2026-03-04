<script setup>
import { computed } from 'vue'
import { Plus, Delete } from '@element-plus/icons-vue'

const props = defineProps({
  modelValue: {
    type: Array,
    default: () => []
  }
})

const emit = defineEmits(['update:modelValue'])

// Use computed to access props directly
const variables = computed(() => props.modelValue)

const addVariable = () => {
  const newVars = [...props.modelValue, { key: '', value: '' }]
  emit('update:modelValue', newVars)
}

const removeVariable = (index) => {
  const newVars = [...props.modelValue]
  newVars.splice(index, 1)
  emit('update:modelValue', newVars)
}
</script>

<template>
  <el-card class="variable-panel">
    <template #header>
      <div class="card-header">
        <span>变量管理 (Variables)</span>
        <el-button :icon="Plus" circle size="small" type="primary" @click="addVariable" />
      </div>
    </template>
    
    <div class="variable-list">
      <div v-for="(item, index) in variables" :key="index" class="variable-item">
        <el-input v-model="item.key" placeholder="Key" size="small" class="var-input" />
        <span class="separator">=</span>
        <el-input v-model="item.value" placeholder="Value" size="small" class="var-input" />
        <el-button :icon="Delete" circle size="small" type="danger" text @click="removeVariable(index)" />
      </div>
      <el-empty v-if="variables.length === 0" description="暂无变量" :image-size="60" />
    </div>
  </el-card>
</template>

<style scoped>
.variable-panel {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.variable-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  overflow-y: auto;
  max-height: 100%;
}

.variable-item {
  display: flex;
  align-items: center;
  gap: 5px;
}

.var-input {
  flex: 1;
}

.separator {
  color: #909399;
}
</style>
