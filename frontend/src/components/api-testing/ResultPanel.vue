<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { pathLabel, fieldName, shortValue, flattenFields, statusType, statusLabel, assertionLabel } from '@/utils/apiTesting'
const props=defineProps({result:Object,showReferences:{type:Boolean,default:true},sources:{type:Array,default:()=>[]},editable:Boolean,pending:Boolean,allowAuth:Boolean})
const emit=defineEmits(['add-assertion','use-auth','jump-source'])
const tab=ref('response'),view=ref('tree'),detailTab=ref('request')
const pretty=value=>JSON.stringify(value,null,2)
const checks=computed(()=>props.result?.detail?.assertions||[])
const unchecked=computed(()=>props.result?.status==='UNCHECKED'||(props.result?.status==='PASS'&&!!props.result?.detail?.response&&!checks.value.length))
const resultStatus=computed(()=>unchecked.value?'UNCHECKED':props.result?.status)
const errorCategory=computed(()=>{
  const category=props.result?.detail?.error_category||props.result?.error_category
  if(!category)return ''
  return ({configuration:'配置错误',config:'配置错误',precheck:'配置错误',reference:'引用错误',connection:'连接异常',network:'网络异常',timeout:'请求超时',http:'HTTP 异常',assertion:'业务校验失败',validation:'业务校验失败',unchecked:'未校验',cancelled:'运行已中止',canceled:'运行已中止',runtime:'执行异常',response_size:'响应过大',response_too_large:'响应过大',internal:'执行异常'})[String(category).toLowerCase()]||'执行异常'
})
const orderedChecks=computed(()=>[...checks.value].sort((a,b)=>Number(a.passed)-Number(b.passed)))
const body=computed(()=>props.result?.detail?.response?.body)
const fields=computed(()=>props.result?.detail?.fields||[])
const bodyTree=computed(()=>fields.value.filter(f=>f.path[0]==='body'))
const expanded=computed(()=>flattenFields(bodyTree.value).filter(f=>f.path.length<3).map(f=>JSON.stringify(f.path)))
const tree=computed(()=>{const build=nodes=>nodes.map(n=>({...n,key:JSON.stringify(n.path),label:fieldName(n.path),children:build(n.children||[])}));return build(bodyTree.value)})
watch(()=>[props.result?.step_id,props.result?.status,props.pending],()=>{tab.value=props.result?.status==='FAIL'&&!props.pending?'checks':'response'},{immediate:true})
function fieldWithValue(field){let value=props.result.detail.response;for(const part of field.path)value=value[part];return {...field,example:value}}
async function copyValue(value){try{await navigator.clipboard.writeText(typeof value==='string'?value:pretty(value));ElMessage.success('已复制')}catch{ElMessage.error('复制失败，请手动复制')}}
const sourceName=id=>props.sources.find(s=>s.id===id)?.name||id
defineExpose({showAssertions:()=>{tab.value='checks'}})
</script>
<template>
  <div v-if="result" class="result-panel">
    <div class="result-heading"><el-tag v-if="pending" type="warning">校验待更新</el-tag><el-tag v-else :type="unchecked?'warning':statusType(resultStatus)">{{ unchecked?'未校验':statusLabel(resultStatus) }}</el-tag><el-tag v-if="(result.detail?.attempts||1)>1" type="warning" size="small">共尝试 {{ result.detail.attempts }} 次</el-tag><b v-if="result.detail?.response">HTTP {{ result.detail.response.status_code }}</b><span>{{ Math.round(result.duration_ms||0) }} ms</span><span v-if="checks.length">{{ checks.filter(a=>a.passed).length }}/{{ checks.length }} 条校验通过</span><span v-else-if="result.detail?.response" class="unchecked-note">请求已完成，未配置校验</span></div>
    <el-alert v-if="result.detail?.retry_history?.length" :title="`前 ${result.detail.retry_history.length} 次连接失败后重试：${result.detail.retry_history.map(item=>'第 '+item.attempt+' 次 '+item.error).join('；')}`" type="warning" :closable="false" />
    <el-alert v-if="result.detail?.error&&!pending" :title="errorCategory ? errorCategory+'：'+result.detail.error : result.detail.error" type="error" :closable="false" />
    <div v-if="$slots.tools" class="panel-tools"><slot name="tools" /></div>
    <el-tabs v-model="tab">
      <el-tab-pane label="响应正文" name="response">
        <div class="response-toolbar"><el-radio-group v-if="bodyTree.length" v-model="view" size="small"><el-radio-button value="tree">字段</el-radio-button><el-radio-button value="json">JSON</el-radio-button></el-radio-group><div class="response-tools"><el-button v-if="result.detail?.response" link @click="copyValue(body===undefined?result.detail.response.text:body)">复制正文</el-button></div></div>
        <el-tree v-if="view==='tree'&&bodyTree.length" :data="tree" node-key="key" :default-expanded-keys="expanded" :expand-on-click-node="false">
          <template #default="{data}"><div class="response-field"><span class="field-name" :title="pathLabel(data.path)">{{ data.label }}</span><span class="example" :title="Object.hasOwn(data,'example')?String(data.example):''">{{ Object.hasOwn(data,'example')?shortValue(data.example):'' }}</span><el-button v-if="Object.hasOwn(data,'example')" link size="small" @click.stop="copyValue(data.example)">复制</el-button><el-button v-if="editable" link size="small" type="primary" @click.stop="emit('add-assertion',fieldWithValue(data))">添加校验</el-button><el-button v-if="allowAuth&&!['object','array','null'].includes(data.type)" link size="small" @click.stop="emit('use-auth',data)">用于鉴权</el-button></div></template>
        </el-tree>
        <pre v-else>{{ body===undefined ? result.detail?.response?.text || '没有响应体' : pretty(body) }}</pre>
      </el-tab-pane>
      <el-tab-pane :label="'校验结果'+(checks.length?' ('+checks.length+')':'')" name="checks"><el-alert v-if="pending" title="校验规则已修改，请点击“重新校验响应”。不会重新发送请求。" type="info" :closable="false" /><div v-for="row in orderedChecks" :key="row.id" class="assertion-result"><div><el-tag :type="row.passed?'success':'danger'" size="small">{{ row.passed?'通过':'失败' }}</el-tag> {{ pathLabel(row.path) }} {{ assertionLabel(row.op) }}</div><div class="comparison"><span>期望：{{ row.op==='is_2xx'?'200–299':['exists','not_empty'].includes(row.op)?'—':pretty(row.expected) }}</span><span>实际：{{ row.missing?'字段不存在':pretty(row.actual) }}</span></div><small v-if="!row.passed">{{ row.message }}</small></div><p v-if="!checks.length" class="empty-hint">{{ result.detail?.response?'未配置校验，可从响应字段添加业务校验。':'此步骤没有校验结果。' }}</p></el-tab-pane>
      <el-tab-pane label="详情" name="details">
        <el-select v-model="detailTab" class="detail-select" size="small" aria-label="查看执行详情"><el-option label="请求详情" value="request" /><el-option label="响应头" value="headers" /><el-option label="Cookie" value="cookies" /><el-option label="原始文本" value="text" /><el-option v-if="showReferences" label="引用来源" value="references" /></el-select>
        <pre v-if="detailTab==='request'">{{ pretty(result.detail?.request)||'暂无请求详情' }}</pre><pre v-else-if="detailTab==='headers'">{{ pretty(result.detail?.response?.headers)||'暂无响应头' }}</pre><pre v-else-if="detailTab==='cookies'">{{ pretty(result.detail?.response?.cookies)||'暂无 Cookie' }}</pre><pre v-else-if="detailTab==='text'">{{ result.detail?.response?.text||'空响应' }}</pre>
        <template v-else-if="detailTab==='references'&&showReferences"><div v-for="(item,i) in result.detail?.references||[]" :key="i" class="assertion-result"><el-button link type="primary" @click="emit('jump-source',item.step_id)">{{ sourceName(item.step_id) }} → {{ pathLabel(item.path) }}</el-button><pre>{{ pretty(item.value) }}</pre></div><p v-if="!result.detail?.references?.length" class="empty-hint">未使用前序步骤输出。</p></template>
      </el-tab-pane>
    </el-tabs>
  </div>
  <p v-else class="empty-hint">发送请求后，在这里查看响应、选择字段并添加校验。</p>
</template>
<style scoped>
.panel-tools{display:flex;align-items:center;gap:8px;margin:0 0 10px;flex-wrap:wrap}.empty-hint{padding:12px 0;margin:0;color:var(--ad-muted);font-size:12px;line-height:22px}.unchecked-note{color:var(--ad-warning)}.detail-select{width:160px;margin:4px 0}.response-tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.response-tools .el-button+.el-button{margin-left:0}.field-name{max-width:40%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.response-toolbar{gap:8px;flex-wrap:wrap}
.result-panel{min-width:0}.result-heading{display:flex;gap:14px;align-items:center;margin:0 0 10px;font-size:12px;flex-wrap:wrap}.response-toolbar{display:flex;justify-content:space-between;margin-bottom:10px}.response-field{display:flex;gap:8px;align-items:center;min-width:0;width:100%;font-size:12px}.example{color:var(--ad-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}.response-field .el-button{margin:0}.assertion-result{padding:12px;border-bottom:1px solid var(--ad-border);font-size:13px}.comparison{display:flex;gap:24px;margin-top:8px;flex-wrap:wrap;overflow-wrap:anywhere}small{color:var(--ad-danger)}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--ad-bg);padding:12px;border-radius: var(--ad-radius);max-height:400px;overflow:auto;font-size:12px}.result-panel :deep(.el-tree-node__content){height:32px}.result-panel :deep(.el-tabs__content){overflow:visible}
@media(max-width:760px){.empty-hint,.result-heading,.response-field,.example,.assertion-result,pre{font-size:14px}.response-field{flex-wrap:wrap;gap:4px 8px;min-height:44px}.field-name{max-width:100%}.result-panel :deep(.el-tree-node__content){height:auto;min-height:44px}.response-field .example{min-width:60px}}
</style>
