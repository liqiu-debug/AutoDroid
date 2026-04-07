<script setup>
import { computed } from 'vue'
import { VueDraggable } from 'vue-draggable-plus'
import { Plus, Delete, Edit } from '@element-plus/icons-vue'

const props = defineProps({
  modelValue: {
    type: Array,
    default: () => []
  }
})

const emit = defineEmits(['update:modelValue'])

const steps = computed({
  get: () => props.modelValue,
  set: (val) => emit('update:modelValue', val)
})

const addStep = () => {
  const newStep = { 
    action: 'click', 
    selector: '', 
    value: '',
    description: 'New Step' 
  }
  emit('update:modelValue', [...props.modelValue, newStep])
}

const removeStep = (index) => {
  const newSteps = [...props.modelValue]
  newSteps.splice(index, 1)
  emit('update:modelValue', newSteps)
}
</script>

<template>
  <el-card class="step-list-card">
    <template #header>
      <div class="card-header">
        <span>步骤列表 (Steps)</span>
        <el-button :icon="Plus" type="primary" size="small" @click="addStep">添加步骤</el-button>
      </div>
    </template>
    
    <div class="step-container">
      <VueDraggable 
        v-model="steps" 
        :animation="150" 
        handle=".handle"
        class="drag-area"
      >
        <div v-for="(element, index) in steps" :key="index" class="step-item">
          <div class="handle">⋮⋮</div>
          <div class="step-content">
            <div class="step-row">
              <el-select v-model="element.action" size="small" style="width: 100px">
                <el-option label="Click" value="click" />
                <el-option label="Input" value="input" />
                <el-option label="Wait" value="wait_until_exists" />
                <el-option label="Scroll" value="scroll_to" />
                <el-option label="Text Assert" value="assert_text" />
              </el-select>
              <el-input v-model="element.selector" placeholder="Selector (resourceId/text/xpath)" size="small" />
            </div>
            <div class="step-row" v-if="['input', 'assert_text'].includes(element.action)">
               <el-input v-model="element.value" placeholder="Value" size="small" />
            </div>
          </div>
          <div class="step-actions">
            <el-button :icon="Delete" circle size="small" type="danger" text @click="removeStep(index)" />
          </div>
        </div>
      </VueDraggable>
      
       <el-empty v-if="steps.length === 0" description="拖拽添加或点击上方按钮" />
    </div>
  </el-card>
</template>

<style scoped>
.step-list-card {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.step-container {
  overflow-y: auto;
  flex: 1;
}

.drag-area {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 50px;
}

.step-item {
  display: flex;
  align-items: flex-start;
  padding: 10px;
  background: #fff;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  gap: 10px;
}

.step-item:hover {
  border-color: #409eff;
  box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.1);
}

.handle {
  cursor: move;
  color: #909399;
  font-size: 20px;
  line-height: 1;
  padding-top: 5px;
}

.step-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.step-row {
  display: flex;
  gap: 5px;
}

.step-actions {
  display: flex;
  flex-direction: column;
}
</style>
