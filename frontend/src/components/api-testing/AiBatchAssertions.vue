<script setup>
import { ref, computed, watch, nextTick, onBeforeUnmount } from 'vue'
import api from '@/api'
import { createUuid } from '@/utils/uuid'
import { apiError, copy, batchSuggestionPlan, newAssertions, effectiveStep, statusLabel, statusType } from '@/utils/apiTesting'
import AiSuggestionList from './AiSuggestionList.vue'

// Batch = one through-run + the same per-step generation the single-step dialog
// uses + one grouped review. Generation for every step must finish before any
// assertion is applied: the server refuses to generate for step N once step
// N-1's assertions changed and were not re-verified.
const props = defineProps({ modelValue: Boolean, steps: { type: Array, default: () => [] }, envId: [Number, String], debug: { type: Object, required: true }, editorId: String })
const emit = defineEmits(['update:modelValue', 'update:busy', 'apply'])
const phase = ref('idle'), plan = ref([]), generated = ref({}), chosen = ref({}), progress = ref({ current: 0, total: 0, name: '' })
const outcome = ref(null), error = ref(''), appliedIndexes = ref({})
let alive = true, token = 0
onBeforeUnmount(() => { alive = false; token++ })
const busy = computed(() => ['running', 'generating', 'applying'].includes(phase.value))
watch(busy, value => emit('update:busy', value))
const eligible = computed(() => plan.value.filter(item => item.eligible))
const selectedCount = computed(() => Object.values(chosen.value).reduce((sum, list) => sum + list.length, 0))
const reasonText = item => ({ wait: '等待步骤，无需校验', failed: `${statusLabel(item.status)}，请先修复再生成`, not_executed: item.blockedBy ? `未执行：前序步骤「${plan.value.find(p => p.stepId === item.blockedBy)?.name || ''}」未通过` : '未执行' })[item.reason] || ''
const untilIdle = () => new Promise(resolve => {
  if (!props.debug.running.value) return resolve()
  const stop = watch(props.debug.running, running => { if (!running) { stop(); resolve() } })
})
function feedback(callId, action, count = 0) {
  if (callId) api.apiTesting.post('/ai/feedback', { call_id: callId, action, selected_count: count, modified_count: 0 }).catch(() => {})
}
function reset() { plan.value = []; generated.value = {}; chosen.value = {}; appliedIndexes.value = {}; outcome.value = null; error.value = ''; progress.value = { current: 0, total: 0, name: '' } }
async function start(lastStepId) {
  if (!alive || busy.value) return
  const current = ++token
  reset()
  phase.value = 'running'
  try {
    await props.debug.run(lastStepId, 'through', { confirmed: true })
    await untilIdle()
    if (!alive || current !== token) return
    if (props.debug.error.value) { error.value = props.debug.error.value; phase.value = 'review'; return }
    plan.value = batchSuggestionPlan(props.steps, props.debug.results.value)
    const targets = eligible.value
    phase.value = 'generating'
    progress.value = { current: 0, total: targets.length, name: '' }
    for (const item of targets) {
      if (!alive || current !== token) return
      progress.value = { current: progress.value.current + 1, total: targets.length, name: item.name }
      try {
        const { data } = await api.apiTesting.post('/ai/suggest-assertions', {
          steps: copy(props.steps), step_id: item.stepId, env_id: props.envId || null, debug_session_id: props.debug.id.value,
          editor_id: props.editorId, goal: '', draft_token: createUuid(),
        }, { timeout: 95000 })
        if (!alive || current !== token) return
        generated.value = { ...generated.value, [item.stepId]: { callId: data.call_id, suggestions: data.suggestions || [], warnings: data.warnings || [], error: '' } }
        // Default to all: the server already filtered dynamic-field equality and
        // unprompted business values, so what remains is structural.
        chosen.value = { ...chosen.value, [item.stepId]: (data.suggestions || []).map((_, index) => index) }
      } catch (err) {
        if (!alive || current !== token) return
        generated.value = { ...generated.value, [item.stepId]: { callId: '', suggestions: [], warnings: [], error: apiError(err) } }
      }
    }
    phase.value = 'review'
  } catch (err) {
    if (alive && current === token) { error.value = apiError(err); phase.value = 'review' }
  }
}
function setChosen(stepId, indexes) { chosen.value = { ...chosen.value, [stepId]: indexes } }
function toggleAll(stepId) {
  const all = (generated.value[stepId]?.suggestions || []).map((_, index) => index)
  setChosen(stepId, (chosen.value[stepId] || []).length === all.length ? [] : all)
}
async function applyAll() {
  if (!alive || phase.value !== 'review' || !selectedCount.value) return
  const current = token
  let skipped = 0
  const payload = eligible.value.flatMap(item => {
    const result = generated.value[item.stepId], indexes = chosen.value[item.stepId] || []
    if (!result) return []
    const step = props.steps.find(row => row.id === item.stepId)
    // Idempotent per step: checks the step already has are skipped, not appended.
    const additions = newAssertions(step ? effectiveStep(step).assertions : [], indexes.map(index => ({ ...copy(result.suggestions[index].assertion), id: createUuid() })))
    skipped += indexes.length - additions.length
    appliedIndexes.value = { ...appliedIndexes.value, [item.stepId]: indexes }
    feedback(result.callId, additions.length ? 'accepted' : 'dismissed', additions.length)
    return additions.length ? [{ stepId: item.stepId, name: item.name, assertions: additions }] : []
  })
  phase.value = 'applying'
  emit('apply', payload)
  await nextTick()
  const summary = { applied: payload.length, skipped, rechecked: 0, failedAt: null }
  for (const item of payload) {
    if (!alive || current !== token) return
    const { status, error: message } = await props.debug.recheck(item.stepId, { silent: true })
    if (status !== 'PASS') { summary.failedAt = { ...item, status, message }; break }
    summary.rechecked += 1
  }
  if (!alive || current !== token) return
  outcome.value = summary
  phase.value = 'done'
}
function close() {
  if (phase.value === 'review') Object.values(generated.value).forEach(result => feedback(result.callId, 'dismissed'))
  token++
  phase.value = 'idle'
  emit('update:modelValue', false)
}
defineExpose({ start })
</script>
<template>
  <el-drawer class="ad-drawer" :model-value="modelValue" title="AI 校验建议（全部步骤）" size="min(720px,96vw)" append-to-body :close-on-click-modal="!busy" :close-on-press-escape="!busy" :show-close="!busy" @update:model-value="close">
    <p class="hint">先从头真实执行全部步骤，再为每个拿到响应的步骤生成建议；建议只进入草稿，应用后自动重新校验（不重发请求），最后请保存场景。</p>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <div v-if="phase === 'running'" v-loading="true" class="stage">正在从头执行场景…</div>
    <div v-else-if="phase === 'generating'" v-loading="true" class="stage">正在生成建议 {{ progress.current }}/{{ progress.total }} · {{ progress.name }}</div>
    <template v-else-if="phase === 'review' || phase === 'applying' || phase === 'done'">
      <el-alert v-if="phase === 'done' && outcome" :type="outcome.failedAt ? 'warning' : 'success'" :closable="false" :title="outcome.failedAt ? `已应用 ${outcome.applied} 步，第「${outcome.failedAt.name}」步校验未通过（${outcome.failedAt.message || statusLabel(outcome.failedAt.status)}），后续步骤待校验` : `已应用 ${outcome.applied} 步并全部重新校验通过${outcome.skipped ? `（${outcome.skipped} 条已存在跳过）` : ''}，请保存场景`" />
      <section v-for="item in plan" :key="item.stepId" class="step-block">
        <div class="step-heading"><b>{{ item.name }}</b><el-tag v-if="item.status" size="small" :type="statusType(item.status)">{{ statusLabel(item.status) }}</el-tag><span v-if="!item.eligible" class="hint">{{ reasonText(item) }}</span><el-button v-else-if="generated[item.stepId]?.suggestions.length && phase === 'review'" link size="small" @click="toggleAll(item.stepId)">{{ (chosen[item.stepId] || []).length === generated[item.stepId].suggestions.length ? '取消全选' : '全选' }}</el-button></div>
        <template v-if="item.eligible && generated[item.stepId]">
          <el-alert v-if="generated[item.stepId].error" :title="generated[item.stepId].error" type="error" :closable="false" />
          <p v-for="warning in generated[item.stepId].warnings" :key="warning" class="hint">{{ warning }}</p>
          <AiSuggestionList v-if="generated[item.stepId].suggestions.length" :model-value="chosen[item.stepId] || []" :suggestions="generated[item.stepId].suggestions" :disabled="phase !== 'review'" :applied="appliedIndexes[item.stepId] || []" :sources="steps" @update:model-value="setChosen(item.stepId, $event)" />
          <p v-else-if="!generated[item.stepId].error" class="hint">没有可用建议。</p>
        </template>
      </section>
      <p v-if="!eligible.length && !error" class="hint">没有可生成建议的步骤。</p>
    </template>
    <template #footer>
      <el-button :disabled="busy" @click="close">{{ phase === 'done' ? '关闭' : '取消' }}</el-button>
      <el-button v-if="phase === 'review'" type="primary" :disabled="!selectedCount" @click="applyAll">应用选中的 {{ selectedCount }} 条并重新校验</el-button>
    </template>
  </el-drawer>
</template>
<style scoped>
.hint{font-size:12px;color:var(--ad-muted);line-height:1.6}
.stage{min-height:120px;display:flex;align-items:center;justify-content:center;color:var(--ad-muted);font-size:13px}
.step-block{padding:12px 0;border-bottom:1px solid var(--ad-border)}
.step-heading{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:4px}
.step-heading b{font-size:13px}
@media(max-width:760px){.hint,.stage,.step-heading b{font-size:14px}}
</style>
