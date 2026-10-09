<script setup>
import {computed,onMounted,onBeforeUnmount,ref,watch,provide,nextTick} from 'vue'
import {useRoute,useRouter} from 'vue-router'
import {VueDraggable} from 'vue-draggable-plus'
import {ElMessage,ElMessageBox} from 'element-plus'
import api from '@/api'
import {ArrowLeft,Plus,VideoPlay,Upload} from '@element-plus/icons-vue'
import AssetMetadata from '@/components/api-testing/AssetMetadata.vue'
import EnvironmentDrawer from '@/components/api-testing/EnvironmentDrawer.vue'
import ScheduleDrawer from '@/components/api-testing/ScheduleDrawer.vue'
import {createUuid} from '@/utils/uuid'
import {apiError,blankConfig,cloneStep,copy,effectiveStep,overridesFor,pathLabel,mergeResponseFields,stepFromInterface,localRequestStep,referenceIssues,statusLabel,statusType,sourceLabel,locationLabel,previewValue,assertionLabel,stepSummary,conflictKey} from '@/utils/apiTesting'
import {useUnsavedGuard} from '@/composables/useUnsavedGuard'
import {useApiDebug} from '@/composables/useApiDebug'
import RequestEditor from '@/components/api-testing/RequestEditor.vue'
import ResultPanel from '@/components/api-testing/ResultPanel.vue'
import CurlImportDialog from '@/components/api-testing/CurlImportDialog.vue'
import ImportSpecDialog from '@/components/api-testing/ImportSpecDialog.vue'
import SaveInterfaceDialog from '@/components/api-testing/SaveInterfaceDialog.vue'
import AiAssertions from '@/components/api-testing/AiAssertions.vue'
import AiBatchAssertions from '@/components/api-testing/AiBatchAssertions.vue'
const route=useRoute(),router=useRouter(),id=ref(route.params.id)
const form=ref({name:'',description:'',folder_id:Number(route.query.folder_id)||null,env_id:null,steps:[]})
const metadata=ref({}),requestInvalid=ref(false),requestEditor=ref(null),resultPanel=ref(null)
const saved=ref(''),loading=ref(false),runStarting=ref(false),debugStarting=ref(false),interfaces=ref([]),environments=ref([]),variables=ref([]),folders=ref([]),selectedId=ref(null),notify=ref(false)
let alive=true
onBeforeUnmount(()=>{alive=false})
const temporaryEnvironment=ref(false),temporaryEnvId=ref(null)
const envId=computed(()=>temporaryEnvironment.value?temporaryEnvId.value:form.value.env_id)
const nameInput=ref(null),curlOpen=ref(false),exportOpen=ref(false),exportStep=ref(null),specOpen=ref(false)
const envName=computed(()=>environments.value.find(e=>e.id===envId.value)?.name||'未选择环境')
const steps=computed(()=>form.value.steps),debug=useApiDebug(envId,steps,{retainDisplay:true,envName:()=>envName.value})
const selectedIndex=computed(()=>steps.value.findIndex(s=>s.id===selectedId.value)),selected=computed(()=>steps.value[selectedIndex.value])
// One deep copy per step per render pass instead of one per rendered field.
const summaries=computed(()=>Object.fromEntries(steps.value.map(step=>[step.id,stepSummary(step)])))
const selectedConfig=computed({get:()=>selected.value?effectiveStep(selected.value):blankConfig(),set:value=>{if(selected.value)selected.value.overrides=overridesFor(selected.value.snapshot,value)}})
const issues=computed(()=>referenceIssues(steps.value)),precheckIssues=ref([])
const dirty=computed(()=>requestInvalid.value||!!saved.value&&saved.value!==JSON.stringify(form.value))
const infoOpen=ref(false),libraryOpen=ref(false),envOpen=ref(false),scheduleOpen=ref(false),runOptions=ref(false),notificationConfigured=ref(false)
const aiAvailable=ref(false),batchOpen=ref(false),batchBusy=ref(false),batchRef=ref(null)
const search=ref(''),libraryFolder=ref(null),libraryRows=ref([]),libraryPage=ref(1),libraryTotal=ref(0),libraryChosen=ref([]),libraryBusy=ref(false),libraryKey=ref(0)
const syncDialog=ref(false),syncData=ref(null),choices=ref({})
const authOpen=ref(false),authField=ref(null),authTargets=ref([]),authChoices=ref({}),authMode=ref('bearer'),authHeader=ref('AT')
const resultHeight=ref(270)
let resizeStart=null
function startResize(event){resizeStart={y:event.clientY,height:resultHeight.value};event.currentTarget.setPointerCapture(event.pointerId)}
function resize(event){if(resizeStart)resultHeight.value=Math.max(120,Math.min(420,resizeStart.height+resizeStart.y-event.clientY))}
const hasUpdate=step=>interfaces.value.some(i=>i.id===step.interface_id&&i.version!==step.interface_version)
const result=computed(()=>selected.value?debug.displayResults.value[selected.value.id]:null)
function fieldSource(step){
 const row=debug.displayResults.value[step.id],stored=step.response_schema
 const fields=row?.detail?.fields?.length?row.detail.fields:stored?.fields?.length?stored.fields:interfaces.value.find(i=>i.id===step.interface_id)?.sample_fields||[]
 return {id:step.id,name:step.name,fields:mergeResponseFields(fields),origin:row?(debug.staleIds.value.has(step.id)?'上次调试':'本次调试'):stored?(stored.source==='debug'?'上次调试':'接口样例'):'接口样例',env_name:row?.env_name||stored?.env_name||'',captured_at:row?.captured_at||stored?.captured_at||''}
}
const sources=computed(()=>steps.value.slice(0,selectedIndex.value).filter(s=>s.kind==='request').map(fieldSource))
const currentSource=computed(()=>selected.value?fieldSource(selected.value):{fields:[]})
const exportFields=computed(()=>exportStep.value?fieldSource(exportStep.value).fields:[])
const exportSample=computed(()=>exportStep.value?(debug.displayResults.value[exportStep.value.id]?.detail?.response||interfaces.value.find(i=>i.id===exportStep.value.interface_id)?.sample||null):null)
const overrides=computed(()=>{if(!selected.value)return [];return [...Object.keys(selected.value.overrides.request||{}).map(k=>['request',k]),...Object.keys(selected.value.overrides).filter(k=>k!=='request'&&k!=='sensitive_paths').map(k=>[k])]})
const labels={token:'凭证',username:'用户名',password:'密码',key_name:'请求头 / 参数名',location:'位置',kind:'方式',request:'请求',url:'地址',method:'请求方法',headers:'请求头',query:'查询参数',path_params:'路径参数',body:'请求体',form:'表单',auth:'鉴权',body_type:'请求体格式',timeout_seconds:'超时',assertions:'断言'}
const changeLabel=path=>path.map(k=>labels[k]||k).join(' · ')
function friendly(value){if(Array.isArray(value))return value.map(v=>v?.name?{名称:v.name,值:previewValue(v.value,sources.value),启用:v.enabled}:v?.op?{字段:pathLabel(v.path),条件:assertionLabel(v.op),期望:previewValue(v.expected,sources.value)}:friendly(v));if(value&&typeof value==='object'){if(value.kind&&['literal','env','ref','template','object','array'].includes(value.kind))return previewValue(value,sources.value);return Object.fromEntries(Object.entries(value).map(([k,v])=>[labels[k]||k,friendly(v)]))}return value}
useUnsavedGuard(dirty)
provide('apiSelectStep',selectStep)
provide('apiEditEnvironment',()=>{envOpen.value=true})
let variableRequest=0
async function loadVariables(value=envId.value){const seq=++variableRequest;try{const data=value?(await api.getVariables(value)).data:[];if(seq===variableRequest)variables.value=data}catch(e){if(seq===variableRequest)ElMessage.error(apiError(e))}}
watch(envId,loadVariables)
async function refreshEnvironment(value){await debug.close();environments.value=(await api.getEnvironments()).data;if(temporaryEnvironment.value)temporaryEnvId.value=value;else form.value.env_id=value;await loadVariables(value)}
watch(()=>JSON.stringify(form.value.steps),()=>{precheckIssues.value=[]})
async function load(){
 try{
  const [envs,dirs,notification,ai]=await Promise.all([api.getEnvironments(),api.apiTesting.get('/folders'),api.apiTesting.get('/notification-status'),api.apiTesting.get('/ai/status').then(r=>!!r.data.available).catch(()=>false)])
  environments.value=envs.data;folders.value=dirs.data;notificationConfigured.value=notification.data.configured;aiAvailable.value=ai
  if(id.value){const {data}=await api.apiTesting.get('/scenarios/'+id.value);metadata.value=data;form.value={name:data.name,description:data.description,folder_id:data.folder_id,version:data.version,env_id:data.env_id,steps:data.steps};selectedId.value=steps.value.some(s=>s.id===route.query.step_id)?route.query.step_id:steps.value[0]?.id}
  const ids=[...new Set(steps.value.map(s=>s.interface_id).filter(Boolean))]
  interfaces.value=(await Promise.all(ids.map(i=>api.apiTesting.get('/interfaces/'+i).then(r=>r.data).catch(()=>null)))).filter(Boolean)
  saved.value=JSON.stringify(form.value)
  if(route.query.add_interface){const {data}=await api.apiTesting.get('/interfaces/'+Number(route.query.add_interface));libraryChosen.value=[data];addChosen()}
  if(route.query.open==='schedule')scheduleOpen.value=true
  if(route.query.step_id)await nextTick().then(()=>requestEditor.value?.focus('assertions'))
 }catch(e){ElMessage.error(apiError(e))}
}
function selectStep(stepId){if(requestInvalid.value){ElMessage.warning('请先修正当前步骤的 JSON');return false}selectedId.value=stepId;return true}
async function locate(issue){if(!selectStep(issue.step_id))return;await nextTick();requestEditor.value?.focus(issue.section,issue.location)}
async function loadLibrary(){libraryBusy.value=true;try{const {data}=await api.apiTesting.get('/interfaces',{keyword:search.value,folder_id:libraryFolder.value,skip:(libraryPage.value-1)*30,limit:30});libraryRows.value=data.items;libraryTotal.value=data.total}catch(e){ElMessage.error(apiError(e))}finally{libraryBusy.value=false}}
function openLibrary(){if(requestInvalid.value)return ElMessage.warning('请先修正 JSON');libraryChosen.value=[];libraryKey.value++;libraryOpen.value=true;loadLibrary()}
function addLocal(config=blankConfig(),name='新请求'){if(requestInvalid.value)return ElMessage.warning('请先修正当前 JSON');const step=localRequestStep(config,name);steps.value.push(step);selectedId.value=step.id;libraryOpen.value=false}
function importRequest(config){let name='导入请求';try{name=config.request.method+' '+new URL(config.request.url.value).pathname}catch{}addLocal(config,name)}
function librarySearch(){libraryPage.value=1;loadLibrary()}
function addItems(items){
 for(const item of items){if(!interfaces.value.some(i=>i.id===item.id))interfaces.value.push(item);const step=stepFromInterface(item);if(item.sample_fields?.length)step.response_schema={fields:copy(item.sample_fields),source:'sample',env_id:null,env_name:'',captured_at:''};steps.value.push(step);selectedId.value=step.id}
 libraryOpen.value=false
}
function addChosen(){addItems(libraryChosen.value)}
function addWait(){const step={id:createUuid(),name:'等待',kind:'wait',snapshot:blankConfig(),overrides:{},seconds:1};steps.value.push(step);selectedId.value=step.id;libraryOpen.value=false}
async function stepAction(action,step){
 if(debug.running.value)return
 if(action==='export'){if(requestInvalid.value)return ElMessage.warning('请先修正 JSON');exportStep.value=copy(step);exportOpen.value=true;return}
 if(action==='duplicate'){if(requestInvalid.value)return ElMessage.warning('请先修正 JSON');const next=cloneStep(step);steps.value.splice(steps.value.indexOf(step)+1,0,next);selectedId.value=next.id;return}
 try{await ElMessageBox.confirm('删除步骤「'+step.name+'」？相关引用会标出需要修复的位置。','删除步骤',{type:'warning'});form.value.steps=steps.value.filter(s=>s.id!==step.id);if(selectedId.value===step.id){requestInvalid.value=false;selectedId.value=steps.value[0]?.id}}catch{}
}
function captureSchemas(){
 for(const step of steps.value){const row=debug.displayResults.value[step.id];if(row?.detail?.fields?.length)step.response_schema={fields:copy(row.detail.fields),source:'debug',env_id:row.env_id,env_name:row.env_name||'',captured_at:row.captured_at||''}}
}
async function save(openAfter=null){
 if(!saved.value||loading.value||debug.running.value||debugStarting.value||(runStarting.value&&openAfter!=='run'))return false
 if(!form.value.name.trim()){nameInput.value?.focus();ElMessage.warning('请输入场景名称');return false}
 if(requestInvalid.value){ElMessage.warning('请先修正 JSON');return false}
 if(issues.value.length){await locate(issues.value[0]);ElMessage.warning('请先修复引用');return false}
 loading.value=true
 try{captureSchemas();const submitted=copy(form.value);const {data}=id.value?await api.apiTesting.put('/scenarios/'+id.value,submitted):await api.apiTesting.post('/scenarios',submitted);if(!alive)return false;metadata.value=data;form.value.version=data.version;saved.value=JSON.stringify({...submitted,version:data.version});id.value=data.id;if(openAfter!=='run')await normalizeSavedRoute(openAfter);ElMessage.success('场景已保存');return true}catch(e){if(alive)ElMessage.error(apiError(e));return false}finally{loading.value=false}
}
async function normalizeSavedRoute(openAfter=null){
 if(!alive||!id.value)return
 if(String(route.params.id)!==String(id.value)||route.query.add_interface){const query={...route.query};delete query.add_interface;if(openAfter==='schedule')query.open='schedule';await router.replace({path:'/api-testing/scenarios/'+id.value+'/edit',query})}
}
async function precheck(stepId){
 const targets=stepId?steps.value.slice(0,steps.value.findIndex(s=>s.id===stepId)+1):steps.value
 try{const {data}=await api.apiTesting.post('/precheck',{...form.value,name:form.value.name||'未命名场景',steps:targets,env_id:envId.value,validation_mode:stepId?'debug':'run'});precheckIssues.value=data.errors;if(data.errors.length){await locate(data.errors[0]);return false}return true}catch(e){ElMessage.error(apiError(e));return false}
}
async function run(){
 if(!alive||runStarting.value||loading.value||debugStarting.value||debug.running.value||debug.rechecking.value)return
 runStarting.value=true
 const environment=envId.value,sendNotification=notify.value
 let openedReport=false,shouldNormalize=false
 try{
 if(!form.value.name.trim()){nameInput.value?.focus();ElMessage.warning('请输入场景名称');return}
 if(!await precheck()||!alive)return
 if(dirty.value||!id.value){if(!await save('run'))return;shouldNormalize=true}
 const scenarioId=id.value,version=form.value.version
 try{await ElMessageBox.confirm('将执行已保存场景，使用全新的请求和 Cookie 会话。写入操作会真实生效。','运行场景',{confirmButtonText:'开始运行',type:'warning'});if(!alive)return;const {data}=await api.apiTesting.post('/scenarios/'+scenarioId+'/runs',{env_id:environment,version,notify:sendNotification});if(!alive)return;openedReport=true;await router.push('/execution/reports/api/'+data.id)}catch(e){if(alive&&!['cancel','close'].includes(e))ElMessage.error(apiError(e))}
 }finally{if(alive&&shouldNormalize&&!openedReport)await normalizeSavedRoute();runStarting.value=false}
}
async function runStep(mode){
 if(!alive||debugStarting.value||runStarting.value||loading.value||debug.running.value||debug.rechecking.value||!selected.value)return
 const stepId=selected.value.id,signature=JSON.stringify([steps.value,envId.value])
 debugStarting.value=true
 try{if(await precheck(stepId)&&alive&&signature===JSON.stringify([steps.value,envId.value]))await debug.run(stepId,mode)}finally{debugStarting.value=false}
}
async function schedule(){if((dirty.value||!id.value)&&!await save('schedule'))return;scheduleOpen.value=true}
function moreCommand(command){if(command==='info')infoOpen.value=true;else if(command==='schedule')schedule();else if(command==='batch')startBatch();else runOptions.value=true}
// Batch generation = one through-run + per-step generation + grouped review.
// It re-sends every request, so it gets its own single confirmation here and
// the debug composable skips its per-run prompt.
async function startBatch(){
 if(!alive||batchBusy.value||runStarting.value||loading.value||debugStarting.value||debug.running.value||debug.rechecking.value)return
 if(requestInvalid.value)return ElMessage.warning('请先修正 JSON')
 const last=[...steps.value].reverse().find(s=>s.kind==='request')
 if(!last)return ElMessage.warning('场景还没有请求步骤')
 if(issues.value.length){await locate(issues.value[0]);return ElMessage.warning('请先修复引用')}
 if(!await precheck(last.id)||!alive)return
 try{await ElMessageBox.confirm('将从头真实执行全部步骤（写入会产生业务数据，不会回滚），然后为每个拿到响应的步骤生成校验建议。建议只进入草稿，审阅后需保存场景。','AI 校验建议（全部步骤）',{type:'warning',confirmButtonText:'执行并生成'})}catch{return}
 if(!alive)return
 batchOpen.value=true
 await nextTick()
 batchRef.value?.start(last.id)
}
function applyBatch(payload){
 for(const item of payload){const step=steps.value.find(s=>s.id===item.stepId);if(!step)continue;const config=effectiveStep(step);step.overrides=overridesFor(step.snapshot,{...config,assertions:[...config.assertions,...item.assertions]})}
}
function addAssertion(field){requestEditor.value?.addAssertion(field)}
function applyAssertions(assertions){selectedConfig.value={...copy(selectedConfig.value),assertions};requestEditor.value?.focus('assertions')}
async function recheck(){await debug.recheck(selected.value.id);resultPanel.value?.showAssertions()}
function resetOverride(path){const next=copy(selected.value.overrides);if(path.length===2)delete next.request[path[1]];else delete next[path[0]];selected.value.overrides=next}
async function sync(step){try{syncData.value=(await api.apiTesting.post('/interfaces/'+step.interface_id+'/sync-preview',step)).data;choices.value={};syncDialog.value=true}catch(e){ElMessage.error(apiError(e))}}
function applySync(){if(syncData.value.conflicts.some(p=>!choices.value[conflictKey(p)]))return ElMessage.warning('请逐项处理冲突');const step=copy(syncData.value.step);for(const path of syncData.value.conflicts)if(choices.value[conflictKey(path)]==='template'){if(path.length===2)delete step.overrides[path[0]][path[1]];else delete step.overrides[path[0]]}const index=steps.value.findIndex(s=>s.id===step.id);if(index>=0)steps.value[index]=step;syncDialog.value=false;ElMessage.success('已同步到草稿')}
const authCandidates=computed(()=>steps.value.slice(selectedIndex.value+1).filter(s=>s.kind==='request'))
function authConflict(step){const req=effectiveStep(step).request,key=authHeader.value.trim().toLowerCase();return authMode.value==='bearer'?req.auth.kind!=='none'||req.headers.some(h=>h.name.toLowerCase()==='authorization'):req.headers.some(h=>h.name.toLowerCase()===key)||(key==='authorization'&&['bearer','basic'].includes(req.auth.kind))||(req.auth.kind==='api_key'&&req.auth.location==='header'&&req.auth.key_name.toLowerCase()===key)}
function openAuth(field){authField.value=field;authTargets.value=[];authChoices.value={};authOpen.value=true}
function applyAuth(){
 if(!authTargets.value.length)return ElMessage.warning('请选择后续步骤')
 if(authMode.value==='header'&&!authHeader.value.trim())return ElMessage.warning('请输入请求头名称')
 const targets=authCandidates.value.filter(s=>authTargets.value.includes(s.id))
 if(targets.some(s=>authConflict(s)&&!authChoices.value[s.id]))return ElMessage.warning('请逐项处理已有鉴权冲突')
 const token={kind:'ref',step_id:selected.value.id,path:copy(authField.value.path)}
 for(const step of targets){if(authConflict(step)&&authChoices.value[step.id]==='keep')continue;const config=effectiveStep(step);if(authMode.value==='bearer'){config.request.auth={...config.request.auth,kind:'bearer',token:copy(token)};config.request.headers=config.request.headers.filter(h=>h.name.toLowerCase()!=='authorization')}else{config.request.headers=config.request.headers.filter(h=>h.name.toLowerCase()!==authHeader.value.trim().toLowerCase());config.request.headers.push({name:authHeader.value.trim(),value:copy(token),enabled:true});if((config.request.auth.kind==='api_key'&&config.request.auth.location==='header'&&config.request.auth.key_name.toLowerCase()===authHeader.value.trim().toLowerCase())||(authHeader.value.trim().toLowerCase()==='authorization'&&['bearer','basic'].includes(config.request.auth.kind)))config.request.auth.kind='none'}step.overrides=overridesFor(step.snapshot,config)}
 authOpen.value=false;ElMessage.success('已应用到选中的步骤，请保存场景')
}
onMounted(load)
</script>
<template>
<div class="scenario-editor ad-page" :inert="loading||runStarting||debugStarting||batchBusy">
  <header class="ad-page-header">
    <el-button link :icon="ArrowLeft" @click="router.push('/api-testing/scenarios')">返回</el-button>
    <el-input ref="nameInput" v-model="form.name" class="scenario-name" placeholder="输入场景名称" aria-label="场景名称" maxlength="200" :disabled="debug.running.value" />
    <span class="save-state" role="status">{{ loading ? '保存中…' : runStarting ? '准备运行中…' : dirty ? '未保存' : id ? '已保存' : '新场景' }}</span>
    <el-select v-model="form.env_id" clearable placeholder="默认环境" aria-label="场景默认环境" class="environment" :disabled="debug.running.value"><el-option v-for="env in environments" :key="env.id" :label="env.name" :value="env.id" /></el-select>
    <el-button link @click="envOpen=true">环境变量</el-button>
    <el-tag v-if="temporaryEnvironment" type="warning" closable @close="temporaryEnvironment=false">仅本次：{{ envName }}</el-tag>
    <span class="spacer" />
    <el-button :icon="Upload" :loading="loading" :disabled="debug.running.value" @click="save()">保存</el-button>
    <el-button type="primary" :icon="VideoPlay" :loading="runStarting" :disabled="!steps.length||requestInvalid||debug.running.value||loading" @click="run">{{ dirty||!id?'保存并运行':'运行场景' }}</el-button>
    <el-dropdown trigger="click" @command="moreCommand"><el-button>更多 ▾</el-button><template #dropdown><el-dropdown-menu><el-dropdown-item command="info">场景信息</el-dropdown-item><el-dropdown-item command="schedule">定时与通知</el-dropdown-item><el-dropdown-item command="run">仅本次运行设置</el-dropdown-item><el-dropdown-item v-if="aiAvailable" command="batch" divided :disabled="!steps.length||requestInvalid">AI 校验建议（全部步骤）</el-dropdown-item></el-dropdown-menu></template></el-dropdown>
  </header>
  <p class="scenario-note" :class="{placeholder:!form.description}" role="button" tabindex="0" :title="form.description||''" @click="infoOpen=true" @keydown.enter="infoOpen=true" @keydown.space.prevent="infoOpen=true">{{ form.description || '添加说明，帮助接手的人理解这个场景在测什么' }}</p>
  <div v-if="issues.length||precheckIssues.length" class="issues"><el-button v-for="(issue,i) in [...issues,...precheckIssues]" :key="i" link type="danger" @click="locate(issue)">{{ issue.step_name }} · {{ locationLabel(issue.location) }}：{{ issue.message }} → 定位</el-button></div>
  <div class="workspace">
    <aside class="flow">
      <div class="flow-heading"><b>步骤 ({{ steps.length }})</b><el-button type="primary" link :icon="Plus" :disabled="debug.running.value" @click="openLibrary">添加步骤</el-button></div>
      <VueDraggable v-model="form.steps" handle=".drag-handle" :animation="160" :disabled="debug.running.value||requestInvalid">
        <article v-for="(step,index) in steps" :key="step.id" class="step-card" tabindex="0" role="button" :aria-pressed="step.id===selectedId" @keydown.enter.self="selectStep(step.id)" @keydown.space.self.prevent="selectStep(step.id)" :class="{selected:step.id===selectedId}" @click="selectStep(step.id)">
          <div class="step-heading"><span class="drag-handle">⋮⋮</span><b>{{ index+1 }}. {{ step.name }}</b><el-dropdown trigger="click" :disabled="debug.running.value" @command="stepAction($event,step)"><el-button link @click.stop aria-label="步骤操作">···</el-button><template #dropdown><el-dropdown-menu><el-dropdown-item command="duplicate">复制</el-dropdown-item><el-dropdown-item v-if="step.kind==='request'" command="export">另存到接口库</el-dropdown-item><el-dropdown-item command="remove">删除</el-dropdown-item></el-dropdown-menu></template></el-dropdown></div>
          <p>{{ summaries[step.id] }}</p>
          <el-tag v-if="debug.pendingIds.value.has(step.id)" type="warning" size="small">校验待更新</el-tag><el-tag v-else-if="debug.results.value[step.id]" :type="statusType(debug.results.value[step.id].status)" size="small">{{ statusLabel(debug.results.value[step.id].status) }}</el-tag>
          <el-button v-if="hasUpdate(step)" link type="warning" size="small" @click.stop="sync(step)">接口有更新</el-button>
        </article>
      </VueDraggable>
      <p v-if="!steps.length" class="hint">按业务操作顺序添加请求。</p>
    </aside>
    <section v-if="selected" class="inspector">
      <div class="step-title"><el-input v-model="selected.name" placeholder="步骤名称" aria-label="步骤名称" :disabled="debug.running.value" /><span class="hint">{{ selected.interface_id?'来自接口库':'场景内步骤' }}</span></div>
      <div class="configuration" :inert="debug.running.value">
        <template v-if="selected.kind==='request'">
          <RequestEditor ref="requestEditor" :key="selected.id" v-model="selectedConfig" :sources="sources" :variables="variables" :fields="currentSource.fields" :field-source="currentSource" @invalid-change="requestInvalid=$event">
            <template #actions><el-dropdown split-button type="primary" :disabled="requestInvalid" @click="runStep('single')" @command="runStep('through')">调试本步<template #dropdown><el-dropdown-menu><el-dropdown-item command="through">从头执行到这里</el-dropdown-item></el-dropdown-menu></template></el-dropdown></template>
            <template #retry><el-input-number v-model="selected.retry_count" :min="0" :max="3" aria-label="失败重试次数" /><span class="hint">仅对连接失败重试；已收到响应不会重试，避免重复写入。</span></template>
          </RequestEditor>
          <details v-if="selected.interface_id" class="defaults"><summary>{{ overrides.length?'本步骤修改了 '+overrides.length+' 项配置':'使用接口默认配置' }} · 查看 / 恢复</summary><div v-for="path in overrides" :key="path.join('.')">{{ changeLabel(path) }} <el-button link size="small" @click="resetOverride(path)">恢复接口默认值</el-button></div></details>
        </template>
        <el-form v-else label-position="top"><el-form-item label="等待秒数"><el-input-number v-model="selected.seconds" :min="0" :max="300" /></el-form-item><el-button type="primary" @click="runStep('single')">调试本步</el-button></el-form>
      </div>
      <div class="debug-toolbar">
        <el-button v-if="debug.running.value" type="danger" @click="debug.close">中止调试</el-button>
        <el-button v-if="selected.kind==='request'&&debug.results.value[selected.id]?.detail?.response" :type="debug.pendingIds.value.has(selected.id)||result?.status==='UNCHECKED'?'primary':'default'" :disabled="debug.running.value" :loading="debug.rechecking.value" @click="recheck">重新校验响应</el-button>
        <span v-if="debug.results.value[selected.id]?.detail?.response" class="hint">不重发请求</span><span v-else class="hint">{{ envName }} · 调试将发送真实请求</span>
      </div>
      <el-alert v-if="debug.error.value" :title="debug.error.value" type="error" :closable="false" />
      <div v-if="result" class="resize-handle" role="separator" aria-label="调整响应区高度" aria-orientation="horizontal" tabindex="0" @pointerdown="startResize" @pointermove="resize" @pointerup="resizeStart=null" @pointercancel="resizeStart=null" @keydown.up.prevent="resultHeight=Math.min(420,resultHeight+20)" @keydown.down.prevent="resultHeight=Math.max(120,resultHeight-20)"><span /></div>
      <div class="result-area" :class="{'empty-result':!result}" :style="result?{height:resultHeight+'px'}:{}">
        <p v-if="result" class="source-note">{{ sourceLabel(currentSource) }}{{ debug.staleIds.value.has(selected.id)?' · 仅供参考，请重新调试':'' }}</p>
        <ResultPanel ref="resultPanel" :result="result" :pending="debug.pendingIds.value.has(selected.id)" :sources="sources" editable allow-auth @add-assertion="addAssertion" @use-auth="openAuth" @jump-source="selectStep">
          <template #tools><AiAssertions :key="selected.id" :steps="steps" :step-id="selected.id" :env-id="envId" :debug-session-id="debug.id.value" :editor-id="debug.editorId" :result="debug.results.value[selected.id]" :disabled="loading||debug.running.value||requestInvalid" @update-assertions="applyAssertions" /></template>
        </ResultPanel>
      </div>
    </section>
    <section v-else class="empty-workspace"><h2>从第一个请求开始</h2><p>添加请求、查看响应，再点选字段完成取值和校验。</p><div><el-button type="primary" @click="addLocal()">新建请求</el-button><el-button @click="curlOpen=true">粘贴 cURL</el-button><el-button @click="openLibrary">从接口库选择</el-button></div></section>
  </div>
  <el-drawer class="ad-drawer" v-model="libraryOpen" title="添加步骤" size="min(650px,95vw)">
    <div class="add-entries"><el-button type="primary" plain>接口库</el-button><el-button @click="libraryOpen=false;curlOpen=true">粘贴 cURL</el-button><el-button @click="libraryOpen=false;specOpen=true">导入接口文档</el-button><el-button @click="addLocal()">新建请求</el-button></div>
    <div class="library-tools"><el-input v-model="search" placeholder="搜索接口名称" clearable @keyup.enter="librarySearch" @clear="librarySearch" /><el-select v-model="libraryFolder" clearable placeholder="全部目录" @change="librarySearch"><el-option v-for="f in folders.filter(f=>f.kind==='interface')" :key="f.id" :label="f.name" :value="f.id" /></el-select><el-button @click="librarySearch">搜索</el-button></div>
    <el-table class="ad-table" :key="libraryKey" v-loading="libraryBusy" :data="libraryRows" row-key="id" @selection-change="libraryChosen=$event"><el-table-column type="selection" reserve-selection width="45" /><el-table-column label="接口"><template #default="{row}"><b>{{ row.name }}</b><p class="hint">{{ row.config.request.method }} · {{ previewValue(row.config.request.url) }}</p></template></el-table-column></el-table>
    <el-pagination v-model:current-page="libraryPage" :page-size="30" :total="libraryTotal" layout="prev,pager,next" @current-change="loadLibrary" />
    <template #footer><el-button link @click="addWait">添加等待步骤</el-button><el-button type="primary" :disabled="!libraryChosen.length" @click="addChosen">添加 {{ libraryChosen.length||'' }} 个接口</el-button></template>
  </el-drawer>
  <CurlImportDialog v-model="curlOpen" @import="importRequest" />
  <AiBatchAssertions ref="batchRef" v-model="batchOpen" :steps="steps" :env-id="envId" :debug="debug" :editor-id="debug.editorId" @update:busy="batchBusy=$event" @apply="applyBatch" />
  <ImportSpecDialog v-model="specOpen" notice="导入会先在接口库创建这些接口，再加入当前场景草稿。" @imported="addItems" />
  <SaveInterfaceDialog v-model="exportOpen" :step="exportStep" :sources="steps.map(fieldSource)" :variables="variables" :fields="exportFields" :sample="exportSample" @saved="interfaces.push($event)" />
  <el-drawer class="ad-drawer" v-model="infoOpen" title="场景信息" size="min(520px,95vw)"><el-form label-position="top"><el-form-item label="说明"><el-input v-model="form.description" type="textarea" :rows="3" /></el-form-item><el-form-item label="目录"><el-select v-model="form.folder_id" clearable><el-option v-for="f in folders.filter(f=>f.kind==='scenario')" :key="f.id" :label="f.name" :value="f.id" /></el-select></el-form-item></el-form><p class="hint">配置版本：{{ form.version||'尚未保存' }}</p><AssetMetadata :asset="metadata" /><template #footer><el-button type="primary" @click="infoOpen=false">完成</el-button></template></el-drawer>
  <EnvironmentDrawer v-model="envOpen" :env-id="envId" :environments="environments" :variables="variables" :env-name="envName" @refresh="refreshEnvironment" />
  <ScheduleDrawer v-model="scheduleOpen" :scenario="{...form,id:Number(id)}" :env-id="form.env_id" :environments="environments" :notification-configured="notificationConfigured" />
  <el-dialog class="ad-dialog" v-model="runOptions" title="仅本次运行设置" width="min(480px,95vw)"><el-checkbox v-model="temporaryEnvironment" @change="temporaryEnvId=form.env_id">临时切换环境</el-checkbox><el-select v-if="temporaryEnvironment" v-model="temporaryEnvId" clearable placeholder="不使用环境" aria-label="仅本次环境" style="width:100%;margin:12px 0"><el-option v-for="env in environments" :key="env.id" :label="env.name" :value="env.id" /></el-select><p class="hint">影响本次编辑会话的调试与运行，不修改场景默认环境。切换后需重新调试。</p><el-checkbox v-model="notify" :disabled="!notificationConfigured">发送飞书摘要与报告链接</el-checkbox><p class="hint">{{ notificationConfigured?'接口自动化通知已配置':'通知尚未配置，请管理员在系统设置中配置' }}</p><template #footer><el-button type="primary" @click="runOptions=false">完成</el-button></template></el-dialog>
 <el-dialog class="ad-dialog" v-model="syncDialog" title="同步接口变更" width="min(800px,95vw)"><template v-if="syncData"><p class="hint">逐项确认变化。保留本步骤修改，或使用新的接口默认值。</p><div v-for="change in syncData.changes" :key="JSON.stringify(change.path)" class="change"><b>{{ changeLabel(change.path) }}发生变化</b><div class="diff"><div>原接口配置<pre>{{ JSON.stringify(friendly(change.before),null,2) }}</pre></div><div>新接口配置<pre>{{ JSON.stringify(friendly(change.after),null,2) }}</pre></div></div><el-select v-if="syncData.conflicts.some(p=>conflictKey(p)===conflictKey(change.path))" v-model="choices[conflictKey(change.path)]" placeholder="此项有本步骤修改，请选择"><el-option label="保留本步骤修改" value="keep" /><el-option label="使用新接口默认值" value="template" /></el-select></div></template><template #footer><el-button @click="syncDialog=false">取消</el-button><el-button type="primary" @click="applySync">确认同步到草稿</el-button></template></el-dialog>
 <el-dialog class="ad-dialog" v-model="authOpen" title="应用到后续步骤的鉴权" width="min(680px,95vw)"><p>{{ selected?.name }} → {{ pathLabel(authField?.path) }}</p><el-radio-group v-model="authMode"><el-radio value="bearer">Bearer Token</el-radio><el-radio value="header">自定义请求头</el-radio></el-radio-group><el-input v-if="authMode==='header'" v-model="authHeader" placeholder="例如 AT" style="margin:12px 0" /><div v-for="step in authCandidates" :key="step.id" class="auth-target"><el-checkbox v-model="authTargets" :value="step.id">{{ step.name }}</el-checkbox><el-select v-if="authTargets.includes(step.id)&&authConflict(step)" v-model="authChoices[step.id]" placeholder="已有配置，请选择"><el-option label="保留已有配置" value="keep" /><el-option label="替换为选中的引用" value="replace" /></el-select></div><template #footer><el-button @click="authOpen=false">取消</el-button><el-button type="primary" @click="applyAuth">应用到草稿</el-button></template></el-dialog>
</div>
</template>
<style scoped>
.scenario-editor{padding:0}

.scenario-editor{height:100%;display:flex;flex-direction:column;background:var(--ad-bg);min-width:0}header{display:flex;align-items:center;gap:8px;padding:12px 16px;background:var(--ad-surface);border-bottom:1px solid var(--ad-border);flex-wrap:wrap}header .el-button+.el-button{margin:0}.scenario-name{width:210px}.scenario-name :deep(input){font-weight:600}.environment{width:155px}.spacer{flex:1}.workspace{display:grid;grid-template-columns:230px minmax(0,1fr);flex:1;min-height:0}.flow{padding:12px;overflow:auto;border-right:1px solid var(--ad-border)}.flow-heading{display:flex;justify-content:space-between;align-items:center;margin:0 0 8px;font-size:13px}.step-card{padding:8px 10px;margin-bottom:8px;background:var(--ad-surface);border:1px solid var(--ad-border);border-radius:var(--ad-radius);cursor:pointer}.step-card.selected{border-color:var(--ad-primary);background:var(--ad-primary-soft)}.step-heading{display:flex;align-items:center;gap:8px;font-size:13px}.step-heading b{flex:1;overflow-wrap:anywhere}.step-card p{font-size:12px;color:var(--ad-muted);margin:4px 0}.drag-handle{cursor:grab;color:var(--ad-muted)}.inspector{min-width:0;min-height:0;display:flex;flex-direction:column;background:var(--ad-surface);overflow:hidden}.step-title{display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--ad-border)}.step-title .el-input{max-width:360px}.configuration{flex:1;min-height:100px;overflow:auto;padding:12px 16px}.defaults{font-size:12px;color:var(--ad-muted);margin:16px 0 0}.defaults summary{cursor:pointer}.defaults div{padding:5px 0}.debug-toolbar{display:flex;gap:8px;align-items:center;padding:8px 16px;border-top:1px solid var(--ad-border);flex-shrink:0}.debug-toolbar .el-button+.el-button{margin:0}.result-area{padding:10px 16px;overflow:auto;min-height:100px;max-height:55%;flex-shrink:1}.result-area.empty-result{min-height:0;flex-shrink:0;border-top:1px solid var(--ad-border)}.resize-handle{height:9px;display:flex;align-items:center;justify-content:center;cursor:row-resize;touch-action:none;flex-shrink:0;border-top:1px solid var(--ad-border)}.resize-handle span{width:36px;height:3px;border-radius:2px;background:var(--ad-muted)}.resize-handle:hover span{background:var(--ad-primary)}.source-note{font-size: 12px;color:var(--ad-muted);margin:0 0 8px}.hint{font-size:12px;color:var(--ad-muted);line-height:20px}.issues{max-height:90px;overflow:auto;background:var(--el-color-danger-light-9);padding:8px 16px;display:flex;flex-direction:column;align-items:flex-start}.issues .el-button{margin:0;white-space:normal;text-align:left;height:auto;line-height:22px}.library-tools,.add-entries{display:flex;gap:8px;margin-bottom:16px}.diff{display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:12px;margin:10px 0}.diff pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:220px;overflow:auto;background:var(--ad-bg);padding:12px}.change{margin:20px 0}.auth-target{display:flex;justify-content:space-between;gap:20px;margin:12px 0}.auth-target .el-select{width:230px}.el-drawer .el-select{width:100%}.empty-workspace{display:flex;flex-direction:column;align-items:center;justify-content:center;background:var(--ad-surface);text-align:center;padding:24px}.empty-workspace h2{font-size:20px}.empty-workspace p{font-size:13px;color:var(--ad-muted);margin:0 0 24px}.empty-workspace>div{display:flex;gap:8px;flex-wrap:wrap}.empty-workspace .el-button{margin:0}@media(max-width:1100px){.workspace{grid-template-columns:200px minmax(0,1fr)}.scenario-name{width:170px}}@media(max-width:760px){.workspace{grid-template-columns:155px minmax(0,1fr)}.flow{padding:8px}.step-card{padding:8px}.debug-toolbar,.library-tools{flex-wrap:wrap}.step-title{flex-wrap:wrap}.empty-workspace>div{flex-direction:column}}
.save-state{font-size:12px;color:var(--ad-muted);white-space:nowrap}.scenario-note{margin:0;padding:6px 16px;font-size:12px;color:var(--ad-muted);background:var(--ad-surface);border-bottom:1px solid var(--ad-border);cursor:pointer;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.scenario-note.placeholder{font-style:italic}.scenario-note:focus-visible{outline:2px solid var(--ad-primary);outline-offset:-2px}.scenario-editor>header{margin:0;min-height:56px;padding:12px 16px}.step-card:focus-visible{outline:2px solid var(--ad-primary);outline-offset:2px}.step-card.selected .step-heading{color:var(--ad-primary)}.flow{background:var(--ad-sidebar)}.step-heading b{font-weight:550}.library-tools .el-input,.library-tools .el-select{min-width:0}.source-note{font-size:12px}.step-card p{line-height:20px}.auth-target{flex-wrap:wrap}.diff>div{min-width:0}
@media(max-width:760px){.scenario-editor{overflow:auto}.workspace{display:flex;flex-direction:column;min-height:600px;flex:none}.flow{max-height:210px;border-right:0;border-bottom:1px solid var(--ad-border);flex-shrink:0}.inspector{min-height:460px}.configuration{min-height:200px}.scenario-name{flex:1;min-width:160px}.save-state,.source-note,.hint,.step-card p,.scenario-note{font-size:14px}.scenario-editor>header .spacer{display:none}.diff{grid-template-columns:1fr}.add-entries{flex-wrap:wrap}.library-tools{flex-wrap:wrap}.library-tools .el-input,.library-tools .el-select{flex:1;min-width:120px}.auth-target .el-select{width:100%}.step-card{min-height:44px}}
</style>
