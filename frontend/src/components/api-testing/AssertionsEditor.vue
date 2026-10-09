<script setup>
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import ValueEditor from './ValueEditor.vue'
import ReferencePicker from './ReferencePicker.vue'
import { businessAssertion, copy, pathLabel, flattenFields, literal, fromJson, dynamicField } from '@/utils/apiTesting'
const props = defineProps({ modelValue: {type:Array,default:()=>[]}, fields: Array, sources: Array, variables: Array, fieldSource:Object })
const emit = defineEmits(['update:modelValue','added'])
const picker=ref(false), expectedPicker=ref(false), active=ref(-1)
const rowReferenceOpen=ref(false), rowReferenceIndex=ref(-1)
// Pinning a value that changes every run is the most common source of false
// failures; the automatic suggestion avoids it, manual edits need the reminder.
const dynamicWarning=row=>['eq','ne'].includes(row.op)&&dynamicField(row.path)&&row.expected?.kind==='literal'
function compareWithPredecessor(i){rowReferenceIndex.value=i;rowReferenceOpen.value=true}
function applyPredecessor(value){const rows=copy(props.modelValue),row=rows[rowReferenceIndex.value];if(!row)return;row.op='eq';row.expected=value;emit('update:modelValue',rows);rowReferenceOpen.value=false}
const suggestionOpen=ref(false),suggestionField=ref(null),suggestion=ref(null),suggestionMode=ref('eq'),suggestionReferenceOpen=ref(false)
const addedIndex=ref(null)
const types=[['string','文本'],['number','数字'],['boolean','布尔'],['null','空值'],['object','对象'],['array','数组']]
const operators=[['eq','等于'],['ne','不等于'],['exists','存在'],['not_empty','非空'],['contains','包含'],['gt','大于'],['gte','大于等于'],['lt','小于'],['lte','小于等于'],['type','类型为'],['length','长度等于'],['is_2xx','状态码 2xx']]
const fieldsByPath=computed(()=>new Map(flattenFields(props.fields).map(f=>[JSON.stringify(f.path),f])))
const onlyStatus=computed(()=>props.modelValue.length>0&&props.modelValue.every(row=>JSON.stringify(row.path)==='["status_code"]'))
const structuredExpected=row=>!['exists','not_empty','is_2xx'].includes(row.op)&&['object','array'].includes(row.expected.kind)
function options(row){
  const type=fieldsByPath.value.get(JSON.stringify(row.path))?.type
  return operators.filter(([op])=>op===row.op || ['eq','ne','exists','not_empty','type'].includes(op) || (op==='is_2xx' && JSON.stringify(row.path)==='["status_code"]') || (!type && op!=='is_2xx') || (['gt','gte','lt','lte'].includes(op) && type==='number') || (['contains','length'].includes(op) && ['string','array','object'].includes(type)))
}
function update(i,key,value){const rows=copy(props.modelValue);rows[i][key]=value;emit('update:modelValue',rows)}
function changeOp(i,op){
  const rows=copy(props.modelValue),row=rows[i];row.op=op
  if(op==='length') row.expected=literal(0)
  else if(op==='type') row.expected=literal(fieldsByPath.value.get(JSON.stringify(row.path))?.type || 'string')
  else if(['exists','not_empty','is_2xx'].includes(op)) row.expected=literal(null)
  emit('update:modelValue',rows)
}
function pick(value,field){
  if(active.value<0){add(field);return}
  const rows=copy(props.modelValue), real=props.fieldSource?.origin==='本次调试'
  const replacement=businessAssertion(field,real)
  rows[active.value]={...replacement,id:rows[active.value].id,path:value.path}
  emit('update:modelValue',rows)
}
function add(field){
  addedIndex.value=null
  suggestionField.value=copy(field)
  suggestion.value=businessAssertion(field,props.fieldSource?.origin==='本次调试')
  suggestionMode.value=['eq','not_empty','type'].includes(suggestion.value.op)?suggestion.value.op:'type'
  if(suggestionMode.value==='type'){suggestion.value.op='type';suggestion.value.expected=literal(field.type||'string')}
  suggestionOpen.value=true
}
function chooseSuggestion(mode){
  suggestionMode.value=mode
  suggestion.value.op=mode==='ref'?'eq':mode
  if(mode==='eq')suggestion.value.expected=props.fieldSource?.origin==='本次调试'&&Object.hasOwn(suggestionField.value,'example')?fromJson(suggestionField.value.example):literal('')
  else if(mode==='type')suggestion.value.expected=literal(suggestionField.value.type||'string')
  else suggestion.value.expected=literal(null)
}
function confirmSuggestion(){
  if(suggestionMode.value==='ref'&&suggestion.value.expected.kind!=='ref')return ElMessage.warning('请选择要比较的前序字段')
  const index=props.modelValue.length
  emit('update:modelValue',[...copy(props.modelValue),copy(suggestion.value)])
  suggestionOpen.value=false
  addedIndex.value=index
}
function afterSuggestionClosed(){if(addedIndex.value!==null){emit('added',addedIndex.value);addedIndex.value=null}}
defineExpose({add})
</script>
<template>
  <div class="assertions">
    <div v-if="!modelValue.length" class="check-notice">未配置校验。请求完成后会标记为“未校验”。</div>
    <div v-else-if="onlyStatus" class="check-notice">目前仅校验 HTTP 状态，请补充业务字段校验。</div>
    <div v-for="(row,i) in modelValue" :key="row.id" class="assertion" :class="{'structured-assertion':structuredExpected(row)}" :data-assertion-index="i">
        <el-button class="field-button" :aria-label="`选择校验字段：${pathLabel(row.path)}`" :title="pathLabel(row.path)" @click="active=i;picker=true">{{ pathLabel(row.path) || '选择响应字段' }}</el-button>
        <el-select :model-value="row.op" class="operator" aria-label="校验条件" @update:model-value="changeOp(i,$event)"><el-option v-for="op in options(row)" :key="op[0]" :label="op[1]" :value="op[0]" /></el-select>
      <div class="expected-value" aria-label="期望值">
      <label v-if="structuredExpected(row)" class="expected-label">期望值</label>
      <template v-if="!['exists','not_empty','is_2xx'].includes(row.op)">
        <el-select v-if="row.op==='type'&&row.expected.kind==='literal'" :model-value="row.expected.value" aria-label="期望类型" @update:model-value="update(i,'expected',literal($event))"><el-option v-for="t in types" :key="t[0]" :label="t[1]" :value="t[0]" /></el-select>
        <el-input-number v-else-if="row.op==='length' && row.expected.kind==='literal'" :model-value="Number(row.expected.value)||0" :min="0" :precision="0" aria-label="期望长度" @update:model-value="update(i,'expected',literal($event))" />
        <ValueEditor v-else :model-value="row.expected" :location="['assertions',i,'expected']" :sources="sources" :variables="variables" compact @update:model-value="update(i,'expected',$event)" />
        <el-button v-if="['length','type'].includes(row.op) && row.expected.kind==='literal'" link size="small" @click="active=i;expectedPicker=true">使用变量或引用</el-button>
      </template>
      <span v-else class="hint">{{row.op==='is_2xx'?'HTTP 状态码 200–299':'无需期望值'}}</span>
      </div>
      <el-button class="delete-assertion" link type="danger" @click="emit('update:modelValue',modelValue.filter((_,j)=>j!==i))">删除</el-button>
      <p v-if="dynamicWarning(row)" class="dynamic-warning" role="note">该字段每次运行通常不同，固定值容易误报。<el-button link type="primary" size="small" @click="changeOp(i,'not_empty')">改为「非空」</el-button><el-button v-if="sources?.length" link type="primary" size="small" @click="compareWithPredecessor(i)">与前序字段一致</el-button></p>
    </div>
    <el-button type="primary" plain @click="active=-1;picker=true">+ 添加业务校验</el-button>
    <p v-if="!fields?.length" class="hint">先发送请求或添加响应样例，再选择业务字段。</p>
    <ReferencePicker v-model="picker" field-only :sources="[{...fieldSource,id:'__current__',name:'当前接口响应',fields}]" @select="pick" />
    <ReferencePicker v-model="expectedPicker" :sources="sources" :variables="variables" @select="update(active,'expected',$event)" />
    <el-dialog class="ad-dialog" v-model="suggestionOpen" title="添加业务校验" width="min(560px,94vw)" append-to-body @closed="afterSuggestionClosed">
      <template v-if="suggestion"><p class="suggestion-path">{{ pathLabel(suggestion.path) }}</p><p class="hint">选择要验证的结果。编号、时间和凭证通常适合检查类型或非空。</p>
        <el-radio-group :model-value="suggestionMode" class="suggestion-options" @update:model-value="chooseSuggestion"><el-radio-button value="eq">等于</el-radio-button><el-radio-button value="not_empty">非空</el-radio-button><el-radio-button value="type">类型正确</el-radio-button><el-radio-button v-if="sources?.length" value="ref">与前序字段一致</el-radio-button></el-radio-group>
        <div class="suggestion-expected"><label v-if="suggestionMode!=='not_empty'">期望值</label><ValueEditor v-if="suggestionMode==='eq'" v-model="suggestion.expected" :sources="sources" :variables="variables" compact /><el-select v-else-if="suggestionMode==='type'" v-model="suggestion.expected.value" aria-label="期望类型"><el-option v-for="t in types" :key="t[0]" :label="t[1]" :value="t[0]" /></el-select><el-button v-else-if="suggestionMode==='ref'" class="reference-choice" @click="suggestionReferenceOpen=true">{{ suggestion.expected.kind==='ref' ? (sources.find(s=>s.id===suggestion.expected.step_id)?.name||'前序步骤')+' · '+pathLabel(suggestion.expected.path) : '选择前序步骤字段' }}</el-button><p v-else class="hint">字段存在且内容不为空即可通过，不固定当前响应值。</p></div>
      </template>
      <template #footer><el-button @click="suggestionOpen=false">取消</el-button><el-button type="primary" @click="confirmSuggestion">添加校验</el-button></template>
    </el-dialog>
    <ReferencePicker v-model="suggestionReferenceOpen" :sources="sources" :variables="variables" step-only @select="suggestion.expected=$event" />
    <ReferencePicker v-model="rowReferenceOpen" :sources="sources" :variables="variables" step-only @select="applyPredecessor" />
  </div>
</template>
<style scoped>
.assertions{container-type:inline-size;min-width:0}
.check-notice{font-size:12px;color:var(--ad-warning);background:var(--ad-warning-soft);border:1px solid var(--ad-border);border-radius:6px;padding:10px 12px;margin-bottom:12px;line-height:20px}
.dynamic-warning{grid-column:1/-1;margin:0;font-size:12px;color:var(--ad-warning);background:var(--ad-warning-soft);border:1px solid var(--ad-border);border-radius:6px;padding:6px 10px;line-height:20px;overflow-wrap:anywhere}.dynamic-warning .el-button{margin:0}
.suggestion-path{font-size: 13px;font-weight:600;overflow-wrap:anywhere;margin-top:0}.suggestion-options{margin:10px 0;display:flex;flex-wrap:wrap}.suggestion-expected{margin-top:12px;min-width:0}.suggestion-expected>label{display:block;font-size:12px;color:var(--ad-muted);margin-bottom:8px}.suggestion-expected>.el-select{width:160px}.reference-choice{max-width:100%;height:auto;min-height:32px;white-space:normal;text-align:left;overflow-wrap:anywhere}
.assertion{display:grid;grid-template-columns:minmax(0,1fr) 120px minmax(0,1.25fr) 32px;align-items:start;gap:8px;padding:10px;background:var(--ad-bg);border:1px solid var(--ad-border);border-radius: var(--ad-radius);margin-bottom:10px;scroll-margin-top:12px}
.field-button{width:100%;min-width:0;justify-content:flex-start;white-space:normal;height:auto;min-height:32px;text-align:left;line-height:20px}
.field-button :deep(span){display:block;min-width:0;width:100%;white-space:normal;overflow-wrap:anywhere}
.operator{width:100%;min-width:0}.expected-value{min-width:0}.expected-label{display:block;font-size:12px;color:var(--ad-muted);margin-bottom:8px}
.delete-assertion{margin:0;min-height:32px}.hint{font-size:12px;color:var(--ad-muted);overflow-wrap:anywhere}
.structured-assertion{grid-template-columns:minmax(0,1fr) 120px 32px}
.structured-assertion>.expected-value{grid-column:1/-1;grid-row:2}
.structured-assertion>.delete-assertion{grid-column:3;grid-row:1}
@container(max-width:620px){
  .assertion{grid-template-columns:120px minmax(0,1fr) 32px}
  .field-button{grid-column:1/3;grid-row:1}
  .operator{grid-column:1;grid-row:2}
  .expected-value{grid-column:2/4;grid-row:2}
  .delete-assertion{grid-column:3;grid-row:1}
  .structured-assertion>.expected-value{grid-column:1/-1;grid-row:3}
}
@container(max-width:380px){
  .assertion{grid-template-columns:minmax(0,1fr) 32px}
  .field-button{grid-column:1}.delete-assertion,.structured-assertion>.delete-assertion{grid-column:2}
  .operator{grid-column:1/-1;width:min(160px,100%)}
  .expected-value,.structured-assertion>.expected-value{grid-column:1/-1;grid-row:3}
}
@media(max-width:760px){.hint,.check-notice,.dynamic-warning,.suggestion-expected>label,.expected-label,.suggestion-path{font-size:14px}.delete-assertion,.field-button{min-height:44px}}
</style>
