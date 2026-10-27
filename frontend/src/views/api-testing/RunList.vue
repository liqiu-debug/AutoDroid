<script setup>
import { ref, onMounted, onBeforeUnmount, onActivated, onDeactivated } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import { apiError, activeStatus, statusLabel, statusType, downloadReport } from '@/utils/apiTesting'
import { pollRequestConfig } from '@/utils/polling'
import { useUserStore } from '@/stores/useUserStore'
const router = useRouter(), user = useUserStore(), rows = ref([]), total = ref(0), page = ref(1), keyword = ref(''), status = ref(''), loading = ref(false), loadError = ref(''), downloading = ref(new Set())
let timer, alive = true
async function load(quiet = false) {
  clearTimeout(timer)
  // 页面隐藏：暂停出票（不发 XHR），定时复查、可见即恢复
  if (typeof document !== 'undefined' && document.hidden) { timer = setTimeout(() => load(true), 2000); return }
  if (!quiet) loading.value = true
  loadError.value = ''
  try {
    // quiet=定时复查（真轮询）显式 8s 短超时；首次/手动加载走默认 30s
    const { data } = await api.apiTesting.get('/runs', { keyword: keyword.value, status: status.value || undefined, skip: (page.value - 1) * 20, limit: 20 }, quiet ? pollRequestConfig() : undefined)
    if (!alive) return
    rows.value = data.items; total.value = data.total
    if (rows.value.some(r => activeStatus(r.status) || r.notification_status === 'PENDING')) timer = setTimeout(() => load(true), 2000)
  } catch (err) { loadError.value = apiError(err); ElMessage.error(loadError.value) } finally { loading.value = false }
}
function search() { page.value = 1; load() }
async function download(id) { if(downloading.value.has(id))return;downloading.value.add(id);try { await downloadReport(api.apiTesting, id) } catch (err) { ElMessage.error(apiError(err)) } finally {downloading.value.delete(id)} }
async function remove(row) {
  try { await ElMessageBox.confirm(`删除「${row.scenario_name}」的这次报告？`, '删除报告', { type: 'warning' }); await api.apiTesting.delete(`/runs/${row.id}`); load() }
  catch (err) { if (!['cancel','close'].includes(err)) ElMessage.error(apiError(err)) }
}
onMounted(load)
onActivated(() => { alive = true; load() })
onDeactivated(() => { alive = false; clearTimeout(timer) })
onBeforeUnmount(() => { alive = false; clearTimeout(timer) })
</script>
<template>
  <div class="api-run-list ad-page"><div class="filters ad-toolbar"><el-input v-model="keyword" placeholder="搜索接口场景" clearable style="width:260px" @keyup.enter="search" @clear="search" /><el-select v-model="status" placeholder="全部状态" clearable style="width:150px" @change="search"><el-option v-for="s in ['PASS','FAIL','ERROR','ABORTED','RUNNING','QUEUED']" :key="s" :value="s" :label="statusLabel(s)" /></el-select><el-button @click="search">刷新</el-button><el-button link type="primary" @click="router.push('/api-testing/scenarios')">自动化场景</el-button></div>
    <el-alert v-if="loadError" :title="loadError" type="error" :closable="false"><template #default><el-button link @click="load()">重新加载</el-button></template></el-alert>
    <div class="report-table"><el-table height="100%" class="ad-table" v-loading="loading" :data="rows"><el-table-column label="场景" min-width="180" show-overflow-tooltip><template #default="{row}"><el-button link class="ad-name-button" @click="router.push(`/execution/reports/api/${row.id}`)">{{ row.scenario_name }}</el-button><el-tag v-if="row.task_id" size="small" type="info">定时</el-tag></template></el-table-column><el-table-column label="状态" width="100"><template #default="{row}"><el-tag :type="statusType(row.status)">{{ statusLabel(row.status) }}</el-tag></template></el-table-column><el-table-column prop="env_name" label="环境" min-width="110" /><el-table-column prop="executor_name" label="执行人" width="100" /><el-table-column label="耗时" width="100"><template #default="{row}">{{ (row.duration_ms / 1000).toFixed(2) }} s</template></el-table-column><el-table-column label="开始时间" width="150"><template #default="{row}">{{ row.created_at?.replace('T',' ').slice(0,19) }}</template></el-table-column><el-table-column label="操作" width="118" fixed="right"><template #default="{row}"><div class="ad-row-actions"><el-button link :loading="downloading.has(row.id)" @click="download(row.id)">下载</el-button><el-dropdown trigger="click" @command="remove(row)"><el-button link :aria-label="row.scenario_name+'报告的更多操作'">更多</el-button><template #dropdown><el-dropdown-menu><el-dropdown-item command="delete" :disabled="activeStatus(row.status)||!(user.isAdmin||user.userInfo?.id===row.executor_id)">{{ activeStatus(row.status)?'运行中无法删除':user.isAdmin||user.userInfo?.id===row.executor_id?'删除':'删除（仅执行人或管理员）' }}</el-dropdown-item></el-dropdown-menu></template></el-dropdown></div></template></el-table-column><template #empty><el-empty :description="loadError ? '报告加载失败，请重试' : '运行接口场景后，报告会出现在这里'" /></template></el-table></div>
    <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="total, prev, pager, next" @current-change="load()" />
  </div>
</template>
<style scoped>.api-run-list{padding:0;min-width:0;min-height:0;display:flex;flex-direction:column;flex:1;overflow:hidden}.report-table{flex:1;min-height:0;overflow:hidden}.filters,.el-pagination,.el-alert{flex-shrink:0}.filters{display:flex;gap:8px;margin-bottom:12px;align-items:center;flex-wrap:wrap}.el-pagination{margin-top:12px;justify-content:flex-end}.ad-row-actions{display:flex;gap:12px;align-items:center;white-space:nowrap}.ad-row-actions .el-button{margin:0}.ad-name-button{display:block;max-width:100%;overflow:hidden;text-overflow:ellipsis;font-size:12px}.ad-table{width:100%}@media(max-width:760px){.filters .el-input{width:100%!important}.filters .el-select{flex:1;min-width:100px}.ad-name-button{font-size:14px}.el-pagination{justify-content:flex-start;overflow:auto}}</style>
