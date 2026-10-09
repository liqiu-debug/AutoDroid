import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import { createUuid } from '@/utils/uuid'
import { apiError, copy, requestSignatures, assertionSignature } from '@/utils/apiTesting'

export function useApiDebug(envId, steps, { retainDisplay = false, envName = () => '' } = {}) {
  const id = ref(null), running = ref(false), rechecking = ref(false), error = ref(''), cache = ref({})
  // Display history is never submitted as execution data or restored to results.
  const history = ref({})
  const signatures = computed(() => requestSignatures(steps.value))
  const results = computed(() => Object.fromEntries(Object.entries(cache.value).filter(([key,row]) => row.env_id === (envId.value || null) && row.request_signature === signatures.value[key])))
  const pendingIds = computed(() => new Set(steps.value.filter(s => results.value[s.id] && (results.value[s.id].assertions_pending || results.value[s.id].assertion_signature !== assertionSignature(s))).map(s=>s.id)))
  const displayResults = computed(() => retainDisplay ? { ...history.value, ...results.value } : results.value)
  const staleIds = computed(() => new Set(Object.keys(displayResults.value).filter(key => !results.value[key])))
  const editorId = createUuid()
  let timer, alive = true, generation = 0
  const current = token => alive && token === generation
  const removeSession = sessionId => api.apiTesting.delete(`/debug-sessions/${sessionId}`).catch(() => {})
  const close = async () => {
    generation++
    clearTimeout(timer)
    const previous = id.value
    id.value = null; running.value = false; rechecking.value = false; cache.value = {}
    if (previous) await removeSession(previous)
  }
  watch(envId, close, { flush: 'sync' })
  watch(() => JSON.stringify(steps.value), next => {
    const newer = JSON.parse(next)
    const existing = new Set(newer.map(s => s.id))
    history.value = Object.fromEntries(Object.entries(history.value).filter(([key]) => existing.has(key)))
    cache.value = Object.fromEntries(Object.entries(cache.value).filter(([key,row]) => existing.has(key) && row.request_signature === signatures.value[key]))
    if (running.value) close()
    else if (rechecking.value) { generation++; rechecking.value = false }
  }, { flush: 'sync' })
  const remember = (data, draft, environment, environmentName) => {
    const sigs=requestSignatures(draft)
    const rows=Object.fromEntries(Object.entries(data).filter(([key])=>Object.hasOwn(sigs,key)).map(([key,row])=>[key,{...row, env_id:environment, env_name:environmentName, request_signature:sigs[key], assertion_signature:assertionSignature(draft.find(s=>s.id===key))}]))
    cache.value=rows
    if(retainDisplay) history.value={...history.value,...rows}
  }
  const poll = async (token, sessionId, draft, environment, environmentName) => {
    if (!current(token) || id.value !== sessionId) return
    try {
      const { data } = await api.apiTesting.get(`/debug-sessions/${sessionId}`)
      if (!current(token) || id.value !== sessionId) return
      remember(data.results, draft, environment, environmentName); error.value = data.error || ''
      running.value = data.status === 'RUNNING'
      if (running.value) timer = setTimeout(() => poll(token, sessionId, draft, environment, environmentName), 650)
    } catch (err) {
      if (current(token)) { error.value = apiError(err); await close() }
    }
  }
  // `confirmed` lets a caller that already showed its own real-request warning
  // (the batch assertion flow) skip the per-run confirmation.
  const run = async (stepId, mode = 'single', { confirmed = false } = {}) => {
    if (!alive || running.value || rechecking.value) return
    const draft = copy(steps.value), environment = envId.value || null, environmentName = envName()
    const index = draft.findIndex(s => s.id === stepId)
    if (index < 0) return
    const targets = mode === 'through' ? draft.slice(0, index + 1) : [draft[index]]
    const token = ++generation
    running.value = true
    let submitted = false
    try {
      if (!confirmed) await ElMessageBox.confirm(`将真实执行：${targets.map(s => s.name).join(' → ')}。写入、删除等操作会产生业务数据，停止测试不会回滚。`, mode === 'through' ? '从头执行到此步' : '调试当前步骤', { type: 'warning', confirmButtonText: '确认执行' })
      if (!current(token)) return
      let sessionId = id.value
      if (!sessionId) {
        sessionId = (await api.apiTesting.post('/debug-sessions', { editor_id: editorId, env_id: environment })).data.id
        if (!current(token)) { await removeSession(sessionId); return }
        id.value = sessionId
      }
      await api.apiTesting.post(`/debug-sessions/${sessionId}/execute`, { editor_id: editorId, env_id: environment, steps: draft, step_id: stepId, mode })
      if (!current(token) || id.value !== sessionId) return
      const invalidated = new Set((mode === 'through' ? draft : draft.slice(index)).map(s => s.id))
      cache.value = Object.fromEntries(Object.entries(cache.value).filter(([key]) => !invalidated.has(key)))
      error.value = ''; submitted = true
      await poll(token, sessionId, draft, environment, environmentName)
    } catch (err) { if (current(token) && !['cancel','close'].includes(err)) ElMessage.error(apiError(err)) }
    finally { if (current(token) && !submitted) running.value = false }
  }
  // Returns { status, error } so a batch caller can decide whether to continue
  // without parsing toasts; `silent` suppresses the per-step messages.
  const recheck = async (stepId, { silent = false } = {}) => {
    if(rechecking.value||running.value)return { status: null, error: '调试进行中' }
    if (!id.value || !results.value[stepId]) { if (!silent) ElMessage.warning('请先调试当前请求'); return { status: null, error: '请先调试当前请求' } }
    const token=++generation,draft=copy(steps.value),original=cache.value[stepId],sessionId=id.value,environment=envId.value||null
    rechecking.value=true
    try {
      const {data}=await api.apiTesting.post(`/debug-sessions/${sessionId}/assertions`,{editor_id:editorId,env_id:environment,steps:draft,step_id:stepId})
      if(!current(token)||id.value!==sessionId)return { status: null, error: '调试上下文已改变' }
      const row={...original,...data.result,assertion_signature:assertionSignature(draft.find(s=>s.id===stepId))}
      cache.value={...cache.value,[stepId]:row};history.value={...history.value,[stepId]:row}
      if(!silent)ElMessage[data.result.status==='PASS'?'success':'warning'](data.result.status==='PASS'?'校验通过，未重新发送请求':data.result.status==='UNCHECKED'?'尚无校验规则，请先添加':'校验未通过，请查看结果')
      return { status: data.result.status, error: null }
    }catch(err){const message=apiError(err);if(current(token)&&!silent)ElMessage.error(message);return { status: null, error: message }}finally{if(current(token))rechecking.value=false}
  }
  onBeforeUnmount(() => { alive = false; close() })
  return { id, editorId, running, rechecking, results, displayResults, staleIds, pendingIds, error, run, recheck, close }
}
