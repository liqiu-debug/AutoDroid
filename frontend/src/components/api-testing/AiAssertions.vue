<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import { createUuid } from '@/utils/uuid'
import AiSuggestionList from './AiSuggestionList.vue'
import { apiError, copy, effectiveStep, newAssertions } from '@/utils/apiTesting'
const props=defineProps({steps:Array,stepId:String,envId:[Number,String],debugSessionId:String,editorId:String,result:Object,disabled:Boolean})
const emit=defineEmits(['update-assertions'])
const available=ref(false),open=ref(false),busy=ref(false),applying=ref(false),goal=ref(''),suggestions=ref([]),chosen=ref([]),warnings=ref([]),error=ref(''),callId=ref(''),revision=ref(createUuid()),resultRevision=ref(''),applied=ref(null),appliedIndexes=ref([])
let alive=true,modificationRecorded=false
const recordedFeedback=new Map()
const assertions=computed(()=>{const step=props.steps?.find(s=>s.id===props.stepId);return step?effectiveStep(step).assertions:[]})
const stale=computed(()=>resultRevision.value!==revision.value)
watch(()=>JSON.stringify([props.steps,props.stepId,props.envId,props.editorId,props.debugSessionId,props.result?.captured_at,goal.value]),()=>{revision.value=createUuid()},{flush:'sync'})
function recordModification(){
  const accepted=applied.value
  if(!accepted||accepted.stepId!==props.stepId||modificationRecorded||applying.value)return
  const changed=accepted.assertions.filter(original=>JSON.stringify(assertions.value.find(a=>a.id===original.id))!==JSON.stringify(original)).length
  if(changed){modificationRecorded=true;feedback('modified',accepted.callId,accepted.assertions.length,changed)}
}
watch(()=>JSON.stringify(assertions.value),recordModification)
onMounted(async()=>{try{const response=await api.apiTesting.get('/ai/status');if(alive)available.value=response.data.available}catch{if(alive)available.value=false}})
onBeforeUnmount(()=>{alive=false})
function feedback(action,id=callId.value,count=0,modified=0){
  if(!id)return
  const recorded=recordedFeedback.get(id)||new Set()
  if(recorded.has(action)||(action==='dismissed'&&recorded.has('accepted')))return
  recorded.add(action);recordedFeedback.set(id,recorded)
  api.apiTesting.post('/ai/feedback',{call_id:id,action,selected_count:count,modified_count:modified}).catch(()=>{})
}
async function generate(){
  if(!alive||busy.value||applying.value||props.disabled)return
  const token=revision.value
  if(suggestions.value.length)feedback('dismissed')
  busy.value=true;error.value='';suggestions.value=[];chosen.value=[];appliedIndexes.value=[];warnings.value=[];callId.value='';resultRevision.value=''
  try{
    const {data}=await api.apiTesting.post('/ai/suggest-assertions',{steps:copy(props.steps),step_id:props.stepId,env_id:props.envId||null,debug_session_id:props.debugSessionId,editor_id:props.editorId,goal:goal.value,draft_token:token},{timeout:95000})
    if(!alive)return
    callId.value=data.call_id;resultRevision.value=data.draft_token;suggestions.value=data.suggestions;warnings.value=data.warnings||[]
    if(token!==revision.value)error.value='配置或响应已改变，请重新生成建议。'
  }catch(e){if(alive)error.value=apiError(e)}finally{if(alive)busy.value=false}
}
async function apply(){
  if(!alive||stale.value||props.disabled||busy.value||applying.value||!chosen.value.length||!callId.value)return
  const indexes=[...new Set(chosen.value)].filter(index=>!appliedIndexes.value.includes(index))
  if(!indexes.length||indexes.some(index=>!Number.isInteger(index)||!suggestions.value[index]?.assertion))return
  applying.value=true
  // Idempotent: a check the step already has is skipped instead of appended.
  const additions=newAssertions(assertions.value,indexes.map(index=>({...copy(suggestions.value[index].assertion),id:createUuid()})))
  const skipped=indexes.length-additions.length
  const accepted={callId:callId.value,stepId:props.stepId,assertions:additions}
  try{
    if(additions.length)emit('update-assertions',[...copy(assertions.value),...additions])
    await nextTick()
    if(!alive||props.stepId!==accepted.stepId)return
    appliedIndexes.value=[...new Set([...appliedIndexes.value,...indexes])];chosen.value=[]
    if(additions.length){applied.value=accepted;modificationRecorded=false;feedback('accepted',accepted.callId,additions.length)}
    open.value=false
    ElMessage[additions.length?'success':'warning'](additions.length?`已加入 ${additions.length} 条校验草稿${skipped?`，${skipped} 条已存在跳过`:''}，可重新校验当前响应`:'选中的建议都已存在，未重复添加')
  }finally{applying.value=false}
  recordModification()
}
function undo(){
  const original=applied.value
  if(!alive||props.disabled||applying.value||!original||original.stepId!==props.stepId)return
  if(original.assertions.some(a=>JSON.stringify(assertions.value.find(v=>v.id===a.id))!==JSON.stringify(a)))return ElMessage.warning('建议已被修改，请在校验中逐项调整，避免覆盖后续编辑')
  const ids=new Set(original.assertions.map(a=>a.id));applied.value=null
  emit('update-assertions',assertions.value.filter(a=>!ids.has(a.id)))
  feedback('undone',original.callId,original.assertions.length)
}
function dismiss(){if(callId.value&&suggestions.value.length)feedback('dismissed');open.value=false}
</script>
<template>
  <span v-if="available" class="ai-actions"><el-button link type="primary" :disabled="disabled||!debugSessionId||!result?.detail?.response" @click="open=true">AI 校验建议</el-button><el-button v-if="applied?.stepId===stepId" link :disabled="disabled" @click="undo">撤销 AI 建议</el-button></span>
  <el-dialog class="ad-dialog" v-model="open" title="AI 校验建议" width="min(740px,95vw)" :close-on-click-modal="false" @closed="dismiss">
    <p class="hint">确认业务期望后再采用。生成建议不会发送业务请求。</p>
    <el-input v-model="goal" placeholder="可选：说明想验证的业务规则，例如查询结果应与创建订单一致" maxlength="2000" type="textarea" :rows="2" aria-label="校验目标" />
    <el-button class="generate" :loading="busy" :disabled="disabled||applying" @click="generate">生成建议</el-button>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-alert v-else-if="suggestions.length&&stale" title="配置或响应已改变，请重新生成后再应用。" type="warning" :closable="false" />
    <p v-for="warning in warnings" :key="warning" class="hint">{{ warning }}</p>
    <AiSuggestionList v-model="chosen" :suggestions="suggestions" :disabled="stale" :applied="appliedIndexes" :sources="steps" />
    <template #footer><el-button @click="dismiss">关闭</el-button><el-button type="primary" :loading="applying" :disabled="!chosen.filter(i=>!appliedIndexes.includes(i)).length||stale||busy||disabled" @click="apply">应用选中的 {{ chosen.filter(i=>!appliedIndexes.includes(i)).length }} 条建议</el-button></template>
  </el-dialog>
</template>
<style scoped>.ai-actions{display:inline-flex;gap:8px;flex-wrap:wrap}.generate{margin:12px 0}.hint{font-size:12px;color:var(--ad-muted);line-height:1.6}@media(max-width:760px){.hint{font-size:14px}}
</style>
