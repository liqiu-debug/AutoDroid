<script setup>
import { pathLabel, assertionLabel, previewValue } from '@/utils/apiTesting'
const props = defineProps({ suggestions: { type: Array, default: () => [] }, modelValue: { type: Array, default: () => [] }, disabled: Boolean, sources: { type: Array, default: () => [] }, applied: { type: Array, default: () => [] } })
const isApplied = index => props.applied.includes(index)
const emit = defineEmits(['update:modelValue'])
const hasExpected = op => !['exists', 'not_empty', 'is_2xx'].includes(op)
</script>
<template>
  <el-checkbox-group :model-value="modelValue" class="suggestions" @update:model-value="emit('update:modelValue', $event)">
    <div v-for="(item, index) in suggestions" :key="index" class="suggestion" :class="{ applied: isApplied(index) }">
      <el-checkbox :value="index" :disabled="disabled || isApplied(index)"><b>{{ pathLabel(item.assertion.path) }} {{ assertionLabel(item.assertion.op) }}</b></el-checkbox><el-tag v-if="isApplied(index)" size="small" type="success" class="applied-tag">已应用</el-tag>
      <p v-if="hasExpected(item.assertion.op)">期望：{{ previewValue(item.assertion.expected, sources) }}</p>
      <p>{{ item.reason }}</p>
      <small>依据字段：{{ pathLabel(item.evidence.path) }} · {{ item.evidence.type }}</small>
    </div>
  </el-checkbox-group>
</template>
<style scoped>
/* el-checkbox-group zeroes font-size and line-height for its own layout; the
   prose below each checkbox must restore both or the lines collapse onto
   each other. */
.suggestions{max-height:42vh;overflow:auto;font-size:13px;line-height:1.6}
.suggestion{padding:12px 0;border-bottom:1px solid var(--ad-border)}
.suggestion p{font-size:13px;margin:6px 0;overflow-wrap:anywhere}
.suggestion small{display:block;font-size:12px;color:var(--ad-muted);line-height:1.6}
.suggestion.applied{opacity:.65}.applied-tag{margin-left:8px;vertical-align:middle}
.suggestion :deep(.el-checkbox){height:auto;white-space:normal}
.suggestion :deep(.el-checkbox__label){white-space:normal;overflow-wrap:anywhere}
@media(max-width:760px){.suggestion p,.suggestion small{font-size:14px}.suggestion{padding:12px}}
</style>
