<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import AssetMetadata from '@/components/api-testing/AssetMetadata.vue'
import AddToScenarioDialog from '@/components/api-testing/AddToScenarioDialog.vue'
import ImportSpecDialog from '@/components/api-testing/ImportSpecDialog.vue'
import dayjs from 'dayjs'
import { Plus, Search, Refresh, Document, FolderOpened, VideoPlay } from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/useUserStore'
import { apiError, authLabel, previewValue, statusLabel, statusType, copy } from '@/utils/apiTesting'
const route = useRoute(), router = useRouter(), user = useUserStore()
let alive=true
onBeforeUnmount(()=>{alive=false})
const isInterface = computed(() => route.path.includes('/interfaces'))
const kind = computed(() => isInterface.value ? 'interface' : 'scenario')
const endpoint = computed(() => isInterface.value ? '/interfaces' : '/scenarios')
const title = computed(() => isInterface.value ? '接口管理' : '自动化场景')
const rows = ref([]), folders = ref([]), keyword = ref(''), folderId = ref(null), loading = ref(false), page = ref(1), total = ref(0)
const pageSize = ref(20),joinOpen=ref(false),joinId=ref(null),importOpen=ref(false)
const foldersVisible=ref(true), detailsOpen=ref(false), details=ref(null), openingRun=ref(false), runStage=ref(''), loadError=ref('')
function showDetails(row){details.value=row;detailsOpen.value=true}
function rowAction(action,row){if(action==='details')showDetails(row);else if(action==='join')join(row);else if(action==='copy')duplicate(row);else if(action==='delete')remove(row)}
const environmentName=id=>environments.value.find(env=>env.id===id)?.name||'未选择环境'
function join(row){joinId.value=row.id;joinOpen.value=true}
const runItem=ref(null),runDialog=ref(false),runEnv=ref(null),environments=ref([]),runNotify=ref(false),notificationConfigured=ref(false),running=ref(false)
async function openRun(row){
  if(!alive||openingRun.value||running.value)return
  openingRun.value=true
  try{
    const [envs,notice,scenario]=await Promise.all([api.getEnvironments(),api.apiTesting.get('/notification-status'),api.apiTesting.get(`/scenarios/${row.id}`)])
    if(!alive)return
    runItem.value=copy(scenario.data);runEnv.value=scenario.data.env_id;runNotify.value=false
    environments.value=envs.data;notificationConfigured.value=notice.data.configured;runDialog.value=true
  }catch(e){ElMessage.error(apiError(e))}finally{openingRun.value=false}
}
async function run(){
  if(!alive||running.value||!runItem.value)return
  const scenario=copy(runItem.value),payload={version:scenario.version,env_id:runEnv.value,notify:runNotify.value}
  running.value=true;runStage.value='正在检查配置'
  try{
    const checked=await api.apiTesting.post('/precheck',{name:scenario.name,steps:scenario.steps,env_id:payload.env_id,validation_mode:'run'})
    if(!alive)return
    if(checked.data.errors?.length){ElMessage.warning(checked.data.errors[0].message);return}
    runStage.value='正在提交运行'
    const {data}=await api.apiTesting.post(`/scenarios/${scenario.id}/runs`,payload)
    if(!alive)return
    runDialog.value=false;await router.push(`/execution/reports/api/${data.id}`)
  }catch(e){ElMessage.error(apiError(e))}finally{running.value=false;runStage.value=''}
}
const canDelete = row => user.isAdmin || user.userInfo?.id === row.user_id
const time = value => value ? dayjs(value).format('YYYY-MM-DD HH:mm:ss') : '-'
function create() { router.push({ path: `/api-testing${endpoint.value}/create`, query: { folder_id: folderId.value || undefined } }) }
const folderTree = computed(() => {
  const build = parent => folders.value.filter(f => f.kind === kind.value && f.parent_id === parent).map(f => ({ ...f, children: build(f.id) }))
  return [{ id: null, name: '全部', children: [{ id: 0, name: '未分组' }, ...build(null)] }]
})
async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [items, dirs] = await Promise.all([api.apiTesting.get(endpoint.value, { keyword: keyword.value, folder_id: folderId.value, skip: (page.value - 1) * pageSize.value, limit: pageSize.value }), api.apiTesting.get('/folders')])
    rows.value = items.data.items; total.value = items.data.total; folders.value = dirs.data
  } catch (err) { loadError.value=apiError(err);ElMessage.error(loadError.value) } finally { loading.value = false }
}
function search() { page.value = 1; load() }
async function createFolder() {
  try {
    const { value } = await ElMessageBox.prompt('目录名称（创建在当前选中目录下）', '新增目录', { inputPattern: /\S/, inputErrorMessage: '请输入名称' })
    await api.apiTesting.post('/folders', { name: value, kind: kind.value, parent_id: folderId.value || null }); load()
  } catch (err) { if (!['cancel','close'].includes(err)) ElMessage.error(apiError(err)) }
}
async function folderAction(action, folder) {
  try {
    if (action === 'rename') {
      const { value } = await ElMessageBox.prompt('目录名称', '重命名目录', { inputValue: folder.name, inputPattern: /\S/ })
      await api.apiTesting.put(`/folders/${folder.id}`, { name: value, kind: folder.kind, parent_id: folder.parent_id })
    } else {
      await ElMessageBox.confirm(`删除目录「${folder.name}」？非空目录不会被删除。`, '删除目录', { type: 'warning' })
      await api.apiTesting.delete(`/folders/${folder.id}`); folderId.value = null
    }
    load()
  } catch (err) { if (!['cancel','close'].includes(err)) ElMessage.error(apiError(err)) }
}
function edit(row) { router.push(`/api-testing${endpoint.value}/${row.id}/edit`) }
async function duplicate(row) {
  try {
    const { data } = await api.apiTesting.get(`${endpoint.value}/${row.id}`)
    const payload = { name: `${data.name} 副本`, description: data.description, folder_id: data.folder_id }
    if (isInterface.value) Object.assign(payload, { config: data.config, sample: data.sample })
    else Object.assign(payload, { steps: data.steps, env_id: data.env_id })
    const created = await api.apiTesting.post(endpoint.value, payload); edit(created.data)
  } catch (err) { ElMessage.error(apiError(err)) }
}
async function remove(row) {
  try {
    await ElMessageBox.confirm(`删除「${row.name}」？被引用的记录无法删除。`, '删除', { type: 'warning' })
    await api.apiTesting.delete(`${endpoint.value}/${row.id}`); load()
  } catch (err) { if (!['cancel','close'].includes(err)) ElMessage.error(apiError(err)) }
}
watch(endpoint, () => { folderId.value = null; keyword.value = ''; search() })
onMounted(async()=>{await load();try{environments.value=(await api.getEnvironments()).data}catch{/* list remains usable */}})
</script>
<template>
  <div class="api-assets ad-page">
    <div class="main-layout">
      <aside v-if="foldersVisible" class="folder-aside ad-surface" aria-label="资产目录">
        <div class="folder-heading"><b>{{ isInterface ? '接口目录' : '场景目录' }}</b><el-button link :icon="Plus" aria-label="新建目录" @click="createFolder" /></div>
        <el-tree :data="folderTree" node-key="id" :props="{label:'name'}" default-expand-all highlight-current @node-click="folderId = $event.id; search()">
          <template #default="{data}"><div class="folder-node"><el-icon><FolderOpened /></el-icon><span :title="data.name">{{ data.name }}</span><el-dropdown v-if="data.id" trigger="click" @command="folderAction($event, data)"><el-button link size="small" :aria-label="data.name+'目录操作'" @click.stop>···</el-button><template #dropdown><el-dropdown-menu><el-dropdown-item command="rename">重命名</el-dropdown-item><el-dropdown-item command="delete" :disabled="!canDelete(data)">删除</el-dropdown-item></el-dropdown-menu></template></el-dropdown></div></template>
        </el-tree>
      </aside>
      <main class="content-wrapper ad-surface">
        <div class="toolbar ad-toolbar">
          <div class="left-tools"><el-button :icon="FolderOpened" :aria-expanded="foldersVisible" aria-label="切换目录" @click="foldersVisible=!foldersVisible" /><h1>{{ title }}</h1><el-input v-model="keyword" clearable :placeholder="isInterface ? '搜索接口名称' : '搜索场景名称'" :prefix-icon="Search" class="search-input" aria-label="搜索资产" @keyup.enter="search" @clear="search" /><el-button :icon="Refresh" aria-label="刷新列表" @click="load" /></div>
          <div class="right-tools"><el-button v-if="isInterface" :icon="Document" @click="importOpen=true">导入文档</el-button><el-button :icon="Document" @click="router.push('/execution/reports?tab=api')">报告</el-button><el-button type="primary" :icon="Plus" @click="create">{{ isInterface ? '新建接口' : '新建场景' }}</el-button></div>
        </div>
        <el-alert v-if="loadError" :title="loadError" type="error" :closable="false"><template #default><el-button link @click="load">重新加载</el-button></template></el-alert>
        <div class="table-container">
          <el-table v-loading="loading" :data="rows" height="100%" class="ad-table" @row-dblclick="edit">
            <el-table-column :label="isInterface ? '接口名称' : '场景名称'" min-width="160" show-overflow-tooltip><template #default="{row}"><div class="name-cell"><el-button class="ad-name-button" link @click="edit(row)">{{ row.name }}</el-button><el-button v-if="!isInterface && row.stale_steps" link type="warning" size="small" :title="`有 ${row.stale_steps} 个步骤引用的接口已更新`" @click="edit(row)">接口有更新</el-button></div></template></el-table-column>
            <el-table-column v-if="isInterface" label="方法" width="78"><template #default="{row}"><span class="method">{{ row.config.request.method }}</span></template></el-table-column>
            <el-table-column v-if="isInterface" label="请求地址" min-width="140" show-overflow-tooltip><template #default="{row}"><span class="request-url">{{ previewValue(row.config.request.url) }}</span></template></el-table-column>
            <el-table-column v-if="!isInterface" label="说明" min-width="140" show-overflow-tooltip><template #default="{row}"><span class="description">{{ row.description || '—' }}</span></template></el-table-column>
            <el-table-column v-if="!isInterface" label="步骤" width="56"><template #default="{row}">{{ row.step_count }}</template></el-table-column>
            <el-table-column v-if="!isInterface" label="最近执行" width="96"><template #default="{row}"><el-button v-if="row.last_run" link @click="router.push(`/execution/reports/api/${row.last_run.id}`)"><el-tag :type="statusType(row.last_run.status)" size="small">{{ statusLabel(row.last_run.status) }}</el-tag></el-button><span v-else class="description">未运行</span></template></el-table-column>
            <el-table-column v-if="!isInterface" label="默认环境" min-width="110" show-overflow-tooltip><template #default="{row}">{{ environmentName(row.env_id) }}</template></el-table-column>
            <el-table-column label="更新" width="105"><template #default="{row}"><el-button link class="metadata-trigger" :aria-label="'查看'+row.name+'的详细信息'" @click="showDetails(row)">{{ row.updated_at ? dayjs(row.updated_at).format('MM-DD HH:mm') : '—' }}</el-button></template></el-table-column>
            <el-table-column label="操作" width="125" fixed="right"><template #default="{row}"><div class="ad-row-actions"><el-button v-if="isInterface" link type="primary" @click="edit(row)">调试</el-button><el-button v-else link type="primary" :disabled="openingRun||running" @click="openRun(row)">运行</el-button><el-dropdown trigger="click" @command="rowAction($event,row)"><el-button link :aria-label="row.name+'的更多操作'">更多</el-button><template #dropdown><el-dropdown-menu><el-dropdown-item command="details">详细信息</el-dropdown-item><el-dropdown-item v-if="isInterface" command="join">加入场景</el-dropdown-item><el-dropdown-item command="copy">复制</el-dropdown-item><el-dropdown-item command="delete" divided :disabled="!canDelete(row)">{{ canDelete(row) ? '删除' : '删除（仅创建人或管理员）' }}</el-dropdown-item></el-dropdown-menu></template></el-dropdown></div></template></el-table-column>
            <template #empty><el-empty :description="loadError ? '列表加载失败，请重试' : keyword ? '没有匹配的结果' : isInterface ? '创建第一个接口，开始调试请求' : '创建第一个场景，编排接口测试'" :image-size="64"><el-button v-if="!loadError&&!keyword" type="primary" @click="create">{{ isInterface?'新建接口':'新建场景' }}</el-button></el-empty></template>
          </el-table>
        </div>
        <el-pagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[20,50,100]" :total="total" layout="total, sizes, prev, pager, next, jumper" @current-change="load" @size-change="search" />
      </main>
    </div>
    <AddToScenarioDialog v-model="joinOpen" :interface-id="joinId" />
    <ImportSpecDialog v-model="importOpen" @imported="load()" />
    <el-drawer class="ad-drawer" v-model="detailsOpen" title="资产信息" size="min(480px,95vw)"><template v-if="details"><h2 class="detail-name">{{ details.name }}</h2><p class="description">{{ details.description || '暂无说明' }}</p><el-descriptions :column="1" border><el-descriptions-item label="ID">{{ details.id }}</el-descriptions-item><el-descriptions-item label="版本">{{ details.version }}</el-descriptions-item><el-descriptions-item v-if="isInterface" label="鉴权">{{ authLabel(details.config.request.auth.kind) }}</el-descriptions-item><el-descriptions-item v-if="!isInterface&&details.last_run" label="最近运行">{{ time(details.last_run.created_at) }}</el-descriptions-item></el-descriptions><AssetMetadata :asset="details" /></template><template #footer><el-button @click="detailsOpen=false">关闭</el-button><el-button v-if="details" type="primary" @click="edit(details)">编辑</el-button></template></el-drawer>
    <el-dialog class="ad-dialog" v-model="runDialog" :title="`运行 · ${runItem?.name||'场景'}`" width="min(440px,95vw)" :close-on-click-modal="!running" :close-on-press-escape="!running" :show-close="!running"><el-form label-position="top" :disabled="running"><el-form-item label="本次运行环境"><el-select v-model="runEnv" clearable><el-option v-for="env in environments" :key="env.id" :label="env.name" :value="env.id" /></el-select></el-form-item><el-checkbox v-model="runNotify" :disabled="!notificationConfigured">发送飞书摘要和报告链接</el-checkbox></el-form><p class="description">运行会发送真实请求，写入操作会产生业务数据。</p><template #footer><el-button :disabled="running" @click="runDialog=false">取消</el-button><el-button type="primary" :loading="running" @click="run">{{ runStage||'开始运行' }}</el-button></template></el-dialog>
  </div>
</template>
<style scoped>
.api-assets{height:100%;display:flex;flex-direction:column;background:var(--ad-bg);padding:16px;box-sizing:border-box;min-width:0}
.main-layout{display:flex;flex:1;min-height:0;gap:12px;overflow:hidden}
.folder-aside{width:176px;flex-shrink:0;background:var(--ad-surface);border:1px solid var(--ad-border);border-radius:var(--ad-panel-radius);padding:12px 8px;overflow:auto}
.folder-heading,.folder-node{display:flex;align-items:center;gap:8px;width:100%}.folder-heading{justify-content:space-between;margin-bottom:8px;padding:0 4px;font-size:13px}.folder-node span{flex:1;overflow:hidden;text-overflow:ellipsis;font-size:12px}.folder-node .el-icon{color:var(--ad-muted)}
.content-wrapper{flex:1;min-width:0;background:var(--ad-surface);border:1px solid var(--ad-border);border-radius:var(--ad-panel-radius);display:flex;flex-direction:column;padding:12px;overflow:hidden}
.toolbar,.left-tools,.right-tools{display:flex;align-items:center;gap:8px}.toolbar{justify-content:space-between;margin-bottom:12px;flex-wrap:wrap}.toolbar h1{font-size:20px;line-height:32px;margin:0;font-weight:600;white-space:nowrap}.search-input{width:200px}.table-container{flex:1;min-height:0;margin-top:0}.method{font-family:var(--ad-font-mono,monospace);font-size:12px;font-weight:600;color:var(--ad-muted)}.request-url{font-family:var(--ad-font-mono,monospace);font-size:12px;color:var(--ad-muted)}.description,.metadata-trigger{font-size:12px;color:var(--ad-muted);line-height:1.6}.el-pagination{margin-top:12px;justify-content:flex-end;flex-shrink:0}.detail-name{font-size:20px;overflow-wrap:anywhere}.asset-metadata{margin-top:16px}.ad-name-button{max-width:100%;overflow:hidden;text-overflow:ellipsis;display:block;font-size:12px;text-align:left;height:24px;padding:0}.name-cell{display:flex;align-items:center;gap:8px;min-width:0}.name-cell .ad-name-button{flex:0 1 auto;min-width:0}.name-cell .el-button+.el-button{margin:0;flex:none}.ad-row-actions{display:flex;align-items:center;gap:12px;white-space:nowrap}.ad-row-actions .el-button{margin:0}
@media(max-width:1100px){.search-input{width:160px}.toolbar h1{font-size:18px}.folder-aside{width:152px}}
@media(max-width:760px){.api-assets{padding:12px}.main-layout{flex-direction:column}.folder-aside{width:auto;max-height:160px;flex-shrink:0}.toolbar,.left-tools,.right-tools{flex-wrap:wrap}.search-input{width:100%;min-width:120px;flex:1}.left-tools,.right-tools{width:100%}.right-tools{justify-content:flex-end}.el-pagination{justify-content:flex-start;overflow:auto}.description,.metadata-trigger,.request-url,.folder-node span,.method{font-size:14px}.ad-name-button{font-size:14px;height:44px}.ad-row-actions{gap:8px}}
</style>
