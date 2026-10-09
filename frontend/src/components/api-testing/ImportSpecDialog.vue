<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import { apiError, copy } from '@/utils/apiTesting'
const props = defineProps({ modelValue: Boolean, notice: { type: String, default: '' } })
const emit = defineEmits(['update:modelValue', 'imported'])
const content = ref(''), parsed = ref(null), busy = ref(false), applying = ref(false)
const chosen = ref([]), folders = ref([]), folder = ref(null), search = ref('')
const filtered = computed(() => {
  const rows = parsed.value?.candidates || [], text = search.value.trim().toLowerCase()
  return text ? rows.filter(row => `${row.name} ${row.method} ${row.path}`.toLowerCase().includes(text)) : rows
})
watch(() => props.modelValue, async open => {
  if (!open) return
  content.value = ''; parsed.value = null; chosen.value = []; folder.value = null; search.value = ''
  try { folders.value = (await api.apiTesting.get('/folders')).data.filter(item => item.kind === 'interface') }
  catch { folders.value = [] }
})
async function parse() {
  if (busy.value || !content.value.trim()) return
  const original = content.value
  busy.value = true
  try {
    const { data } = await api.apiTesting.post('/imports/spec', { content: original })
    if (content.value !== original) return
    parsed.value = data; chosen.value = []
  } catch (err) { ElMessage.error(apiError(err)) } finally { busy.value = false }
}
async function apply() {
  if (applying.value || !chosen.value.length) return
  applying.value = true
  try {
    const items = chosen.value.map(row => ({ name: row.name, description: row.summary, config: copy(row.config), sample: row.sample ? copy(row.sample) : null }))
    const { data } = await api.apiTesting.post('/imports/apply', { folder_id: folder.value, items })
    emit('imported', data.created)
    emit('update:modelValue', false)
    ElMessage.success(`已导入 ${data.created.length} 个接口`)
  } catch (err) { ElMessage.error(apiError(err)) } finally { applying.value = false }
}
</script>
<template>
  <el-dialog class="ad-dialog" :model-value="modelValue" title="导入接口文档" width="min(860px,96vw)" @update:model-value="emit('update:modelValue',$event)">
    <p class="hint">支持 OpenAPI 3、Swagger 2.0 和 Postman v2.1（JSON 或 YAML）。只解析，不发送请求；勾选后才会创建接口。{{ notice }}</p>
    <el-input v-model="content" type="textarea" :rows="6" placeholder="粘贴接口文档内容" aria-label="接口文档内容" />
    <el-button class="parse" :loading="busy" :disabled="!content.trim()" @click="parse">解析预览</el-button>
    <template v-if="parsed">
      <div class="import-tools"><el-input v-model="search" placeholder="搜索接口名称或路径" clearable /><el-select v-model="folder" clearable placeholder="导入到未分组"><el-option v-for="item in folders" :key="item.id" :label="item.name" :value="item.id" /></el-select></div>
      <p v-for="warning in parsed.warnings" :key="warning" class="hint">{{ warning }}</p>
      <el-table :data="filtered" row-key="key" height="300" @selection-change="chosen=$event">
        <el-table-column type="selection" reserve-selection width="45" />
        <el-table-column label="接口" min-width="200"><template #default="{row}"><b>{{ row.name }}</b><p class="hint">{{ row.method }} {{ row.path }}</p><p v-for="warning in row.warnings" :key="warning" class="hint">{{ warning }}</p></template></el-table-column>
      </el-table>
    </template>
    <template #footer><el-button @click="emit('update:modelValue',false)">取消</el-button><el-button type="primary" :loading="applying" :disabled="!chosen.length" @click="apply">导入选中的 {{ chosen.length||'' }} 个接口</el-button></template>
  </el-dialog>
</template>
<style scoped>
.parse{margin:12px 0}.import-tools{display:flex;gap:8px;margin:12px 0}.import-tools .el-input{flex:1;min-width:0}.import-tools .el-select{width:200px}.hint{font-size:12px;color:var(--ad-muted);line-height:20px;overflow-wrap:anywhere}.hint+p{margin-top:6px}
@media(max-width:760px){.hint,.import-tools{font-size:14px}.import-tools{flex-wrap:wrap}.import-tools .el-select{width:100%}}
</style>
