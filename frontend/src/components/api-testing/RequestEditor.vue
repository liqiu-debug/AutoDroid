<script setup>
import { computed, ref, provide, watch, nextTick } from 'vue'
import ValueEditor from './ValueEditor.vue'
import ParametersEditor from './ParametersEditor.vue'
import AssertionsEditor from './AssertionsEditor.vue'
import JsonBodyEditor from './JsonBodyEditor.vue'
import { copy, literal, fromJson } from '@/utils/apiTesting'
const props = defineProps({ modelValue: { type: Object, required: true }, sources: { type: Array, default: () => [] }, variables: { type: Array, default: () => [] }, fields: Array, fieldSource:Object, allowStepReferences: { type: Boolean, default: true } })
const emit = defineEmits(['update:modelValue', 'invalid-change'])
provide('apiAllowStepReferences', computed(() => props.allowStepReferences))
const config = computed(() => props.modelValue), req = computed(() => props.modelValue.request), mainTab = ref('request'), tab = ref(props.modelValue.request.body_type !== 'none' ? 'body' : 'params'), root = ref(null)
watch(()=>req.value.body_type, (type,old)=>{if(old==='none' && type!=='none')tab.value='body'})
async function focus(section,location=[]){
  mainTab.value=section==='assertions'?'assertions':'request'
  if(section!=='assertions')tab.value=section
  await nextTick()
  if(location[1]==='body'){jsonEditor.value?.showFields();await nextTick()}
  if(!location.length)return
  const matches=[...(root.value?.querySelectorAll('[data-config-path]')||[])].filter(node=>{
    const path=JSON.parse(node.dataset.configPath)
    return path.length>0&&path.every((part,i)=>part===location[i])
  })
  const target=matches.sort((a,b)=>JSON.parse(b.dataset.configPath).length-JSON.parse(a.dataset.configPath).length)[0]
    ||root.value?.querySelector(section==='assertions'?`[data-assertion-index="${location[1]}"]`:`#pane-${section}`)
  const assertionRow=section==='assertions'&&location.length===2
  target?.scrollIntoView({block:assertionRow?'start':'nearest',inline:'nearest'})
  const control=(assertionRow?target?.querySelector('.field-button'):null)||target?.querySelector('input,textarea')||target?.querySelector('.source-button,.field-button')||target?.querySelector('button')
  control?.focus({preventScroll:true})
}
const jsonEditor=ref(null),assertionsEditor=ref(null)
async function addAssertion(field){mainTab.value='assertions';await nextTick();assertionsEditor.value?.add(field)}
defineExpose({focus,addAssertion})
function setRequest(key, value) { const data = copy(config.value); data.request[key] = value; emit('update:modelValue', data) }
function setConfig(key, value) { emit('update:modelValue', { ...copy(config.value), [key]: value }) }
function setAuth(key, value) { setRequest('auth', { ...copy(req.value.auth), [key]: value }) }
function changeBody(type) {
  emit('invalid-change', false)
  const next = copy(config.value)
  next.request.body_type = type
  if (type === 'text' && (next.request.body.kind !== 'literal' || typeof next.request.body.value !== 'string')) next.request.body = literal('')
  if (type === 'json' && next.request.body.kind === 'literal' && next.request.body.value == null) next.request.body = fromJson({})
  emit('update:modelValue', next)
}
</script>
<template>
  <div ref="root" class="request-editor">
    <el-form label-position="top" class="request-address" scroll-to-error>
      <el-form-item label="请求方法"><el-select :model-value="req.method" @update:model-value="setRequest('method', $event)"><el-option v-for="m in ['GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS']" :key="m" :value="m" /></el-select></el-form-item>
      <el-form-item label="请求地址"><ValueEditor :model-value="req.url" :location="['request','url']" :sources="sources" :variables="variables" text-only @update:model-value="setRequest('url', $event)" /></el-form-item>
      <div v-if="$slots.actions" class="request-actions"><slot name="actions" /></div>
    </el-form>
    <el-tabs v-model="mainTab" class="main-tabs">
      <el-tab-pane label="请求" name="request">
    <el-tabs v-model="tab">
      <el-tab-pane :label="`参数 (${req.path_params.length+req.query.length})`" name="params"><h4>路径参数 ({{ req.path_params.length }})</h4><ParametersEditor :model-value="req.path_params" :location="['request','path_params']" :sources="sources" :variables="variables" @update:model-value="setRequest('path_params', $event)" /><h4>查询参数 ({{ req.query.length }})</h4><ParametersEditor :model-value="req.query" :location="['request','query']" :sources="sources" :variables="variables" @update:model-value="setRequest('query', $event)" /></el-tab-pane>
      <el-tab-pane label="请求体" name="body"><el-radio-group :model-value="req.body_type" @update:model-value="changeBody"><el-radio-button value="none">无</el-radio-button><el-radio-button value="json">JSON</el-radio-button><el-radio-button value="form">表单</el-radio-button><el-radio-button value="text">文本</el-radio-button></el-radio-group>
        <div class="body-content"><JsonBodyEditor ref="jsonEditor" v-if="req.body_type === 'json'" :model-value="req.body" :location="['request','body']" :sources="sources" :variables="variables" @update:model-value="setRequest('body', $event)" @invalid-change="emit('invalid-change', $event)" /><ValueEditor v-if="req.body_type === 'text'" :model-value="req.body" :location="['request','body']" :sources="sources" :variables="variables" text-only @update:model-value="setRequest('body', $event)" /><ParametersEditor v-if="req.body_type === 'form'" :model-value="req.form" :location="['request','form']" :sources="sources" :variables="variables" @update:model-value="setRequest('form', $event)" /></div>
      </el-tab-pane>
      <el-tab-pane :label="`请求头 (${req.headers.length})`" name="headers"><ParametersEditor :model-value="req.headers" :location="['request','headers']" :sources="sources" :variables="variables" @update:model-value="setRequest('headers', $event)" /></el-tab-pane>
      <el-tab-pane label="鉴权" name="auth"><el-select :model-value="req.auth.kind" style="width:180px" @update:model-value="setAuth('kind', $event)"><el-option v-for="a in [['none','无鉴权'],['bearer','Bearer Token'],['basic','Basic'],['api_key','API Key']]" :key="a[0]" :label="a[1]" :value="a[0]" /></el-select>
        <div v-if="['bearer','api_key'].includes(req.auth.kind)" class="body-content"><label>凭证</label><ValueEditor :model-value="req.auth.token" :location="['request','auth','token']" :sources="sources" :variables="variables" text-only @update:model-value="setAuth('token', $event)" /></div>
        <div v-if="req.auth.kind === 'basic'" class="body-content"><label>用户名</label><ValueEditor :model-value="req.auth.username" :location="['request','auth','username']" :sources="sources" :variables="variables" text-only @update:model-value="setAuth('username', $event)" /><label>密码</label><ValueEditor :model-value="req.auth.password" :location="['request','auth','password']" :sources="sources" :variables="variables" text-only @update:model-value="setAuth('password', $event)" /></div>
        <div v-if="req.auth.kind === 'api_key'" class="body-content url-row"><el-input :model-value="req.auth.key_name" placeholder="API Key 名称" @update:model-value="setAuth('key_name', $event)" /><el-select :model-value="req.auth.location" style="width:150px" @update:model-value="setAuth('location', $event)"><el-option label="请求头" value="header" /><el-option label="查询参数" value="query" /></el-select></div>
        <p class="hint">{{ allowStepReferences ? '可使用敏感环境变量或前序登录步骤的输出。' : '可使用敏感环境变量配置凭证。' }}Cookie 可在请求头配置，执行内自动保持。</p>
      </el-tab-pane>
      <el-tab-pane label="高级" name="settings"><label>总超时（秒）</label><el-input-number :model-value="req.timeout_seconds" :min="1" :max="120" @update:model-value="setRequest('timeout_seconds', $event)" /><template v-if="$slots.retry"><label>失败重试</label><div class="retry-row"><slot name="retry" /></div></template></el-tab-pane>
    </el-tabs>
      </el-tab-pane>
      <el-tab-pane :label="`校验 (${config.assertions.length})`" name="assertions"><AssertionsEditor ref="assertionsEditor" :model-value="config.assertions" :fields="fields" :field-source="fieldSource" :sources="sources" :variables="variables" @update:model-value="setConfig('assertions', $event)" @added="focus('assertions',['assertions',$event])" /></el-tab-pane>
    </el-tabs>
  </div>
</template>
<style scoped>
.request-editor{min-width:0}.request-address{display:grid;grid-template-columns:100px minmax(0,1fr) auto;gap:8px;align-items:end}
.request-actions{display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap}.request-actions :deep(.el-button+.el-button){margin-left:0}
.main-tabs :deep(>.el-tabs__header){margin-bottom:10px}.main-tabs :deep(>.el-tabs__header .el-tabs__item){font-weight:600}
.request-address :deep(.el-form-item){margin-bottom:12px;min-width:0}.request-address :deep(.el-form-item__content){display:block;min-width:0}
.request-address :deep(.el-select){width:100%}.request-address :deep(.el-form-item__label){line-height:22px;padding-bottom:8px}
.url-row{display:flex;gap:12px;align-items:flex-start}.body-content{margin-top:16px}.retry-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
h4{font-size:13px;margin:16px 0 12px}label{display:block;color:var(--ad-text);font-size:13px;margin:12px 0 8px}
.hint{color:var(--ad-muted);font-size:12px;line-height:20px}
@media(max-width:1100px){.request-address{grid-template-columns:90px minmax(0,1fr)}.request-actions{grid-column:1/-1;margin-top:-4px}}
@media(max-width:760px){.hint,h4,label{font-size:14px}.request-address{grid-template-columns:110px minmax(0,1fr)}.request-actions{grid-column:1/-1;justify-content:flex-end}.url-row{flex-wrap:wrap}.main-tabs :deep(.el-tabs__item){font-size:14px}}
</style>
