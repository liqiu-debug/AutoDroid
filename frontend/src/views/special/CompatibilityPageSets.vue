<script setup>
import { computed, onActivated, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CopyDocument, Delete, Edit, Plus, Refresh } from '@element-plus/icons-vue'
import api from '@/api'

const pageSets = ref([])
const cases = ref([])
const activeSetId = ref(null)
const loading = reactive({
  page: false,
  saving: false,
})

const form = reactive({
  id: null,
  name: '',
  description: '',
  pages: [],
})

const selectedPageSet = computed(() => pageSets.value.find(item => item.id === activeSetId.value))
const isEditingExisting = computed(() => Boolean(form.id))

const normalizePageForForm = (page = {}) => ({
  key: page.key || '',
  name: page.name || '',
  case_id: page.case_id || '',
  settle_seconds: page.settle_seconds ?? 2,
  required_text: page.required_text || '',
})

const clonePagesForForm = (pages = []) => (
  pages.map(page => normalizePageForForm(page))
)

const loadForm = (item) => {
  if (!item) {
    Object.assign(form, { id: null, name: '', description: '', pages: [normalizePageForForm()] })
    return
  }
  Object.assign(form, {
    id: item.id,
    name: item.name,
    description: item.description || '',
    pages: clonePagesForForm(item.pages || []),
  })
}

const fetchCases = async () => {
  const { data } = await api.getTestCases({ skip: 0, limit: 500 })
  cases.value = data.items || []
}

const fetchPageSets = async () => {
  const { data } = await api.getCompatPageSets()
  pageSets.value = data || []
  if (activeSetId.value && !pageSets.value.some(item => item.id === activeSetId.value)) {
    activeSetId.value = null
  }
  if (!activeSetId.value && pageSets.value.length) {
    activeSetId.value = pageSets.value[0].id
  }
  loadForm(selectedPageSet.value)
}

const refreshAll = async () => {
  loading.page = true
  try {
    await Promise.all([fetchCases(), fetchPageSets()])
  } catch (err) {
    ElMessage.error(err.response?.data?.detail || err.message || '加载页面合集失败')
  } finally {
    loading.page = false
  }
}

const selectPageSet = (item) => {
  activeSetId.value = item.id
  loadForm(item)
}

const createPageSet = () => {
  activeSetId.value = null
  loadForm(null)
}

const copyPageSet = () => {
  const source = selectedPageSet.value || (form.id ? null : form)
  if (!source) return ElMessage.warning('请选择页面合集')
  activeSetId.value = null
  Object.assign(form, {
    id: null,
    name: `${source.name || '页面合集'}_copy`,
    description: source.description || '',
    pages: clonePagesForForm(source.pages || []),
  })
}

const addPageRow = () => {
  form.pages.push(normalizePageForForm())
}

const removePageRow = (index) => {
  form.pages.splice(index, 1)
}

const buildPayload = () => ({
  name: form.name.trim(),
  description: form.description.trim() || null,
  pages: form.pages.map((page) => ({
    key: page.key?.trim() || null,
    name: page.name.trim(),
    case_id: Number(page.case_id),
    settle_seconds: Number(page.settle_seconds || 0),
    required_text: page.required_text?.trim() || null,
  })),
})

const savePageSet = async () => {
  if (!form.name.trim()) return ElMessage.warning('请输入合集名称')
  if (!form.pages.length) return ElMessage.warning('至少添加一个页面')
  for (const page of form.pages) {
    if (!page.name.trim() || !page.case_id) return ElMessage.warning('页面名称和进入用例不能为空')
  }

  loading.saving = true
  try {
    const payload = buildPayload()
    if (form.id) {
      await api.updateCompatPageSet(form.id, payload)
      activeSetId.value = form.id
    } else {
      const { data } = await api.createCompatPageSet(payload)
      activeSetId.value = data.id
    }
    await fetchPageSets()
    ElMessage.success('页面合集已保存')
  } catch (err) {
    const detail = err.response?.data?.detail
    ElMessage.error(typeof detail === 'string' ? detail : err.message || '保存页面合集失败')
  } finally {
    loading.saving = false
  }
}

const deletePageSet = async () => {
  if (!form.id) return ElMessage.warning('请选择要删除的页面合集')
  try {
    await ElMessageBox.confirm(`确定删除页面合集「${form.name}」吗？`, '确认删除', { type: 'warning' })
  } catch {
    return
  }
  await api.deleteCompatPageSet(form.id)
  activeSetId.value = null
  await fetchPageSets()
  ElMessage.success('页面合集已删除')
}

onMounted(refreshAll)
onActivated(refreshAll)
</script>

<template>
  <div class="page-sets-page" v-loading="loading.page">
    <div class="content-wrapper">
      <aside class="sets-panel">
        <div class="panel-header">
          <span class="card-title">页面合集</span>
          <el-button :icon="Plus" type="primary" @click="createPageSet">新建</el-button>
        </div>
        <div class="sets-list">
          <button
            v-for="item in pageSets"
            :key="item.id"
            type="button"
            class="set-item"
            :class="{ active: item.id === activeSetId }"
            @click="selectPageSet(item)"
          >
            <span class="set-name">{{ item.name }}</span>
            <span class="muted-text">{{ item.pages?.length || 0 }} 个页面</span>
          </button>
          <el-empty v-if="!pageSets.length" description="暂无页面合集" :image-size="80" />
        </div>
      </aside>

      <section class="editor-panel">
        <div class="panel-header">
          <div>
            <span class="card-title">{{ isEditingExisting ? '编辑页面合集' : '新建页面合集' }}</span>
            <span class="muted-text detail-title">页面通过已有用例进入，不在这里编辑步骤</span>
          </div>
          <div class="header-actions">
            <el-button :icon="Refresh" @click="refreshAll">刷新</el-button>
            <el-button :icon="CopyDocument" :disabled="!form.name" @click="copyPageSet">复制</el-button>
            <el-button :icon="Delete" type="danger" plain :disabled="!form.id" @click="deletePageSet">删除</el-button>
            <el-button :icon="Edit" type="primary" :loading="loading.saving" @click="savePageSet">保存</el-button>
          </div>
        </div>

        <el-form label-position="left" label-width="90px">
          <div class="form-grid">
            <el-form-item label="合集名称">
              <el-input v-model="form.name" />
            </el-form-item>
            <el-form-item label="描述">
              <el-input v-model="form.description" />
            </el-form-item>
          </div>
        </el-form>

        <div class="page-list-header">
          <span class="card-title">页面</span>
          <el-button :icon="Plus" @click="addPageRow">添加页面</el-button>
        </div>

        <el-table :data="form.pages" border class="pages-table" height="100%">
          <el-table-column label="名称" min-width="150">
            <template #default="{ row }"><el-input v-model="row.name" /></template>
          </el-table-column>
          <el-table-column label="进入用例" min-width="240">
            <template #default="{ row }">
              <el-select v-model="row.case_id" filterable class="full-control">
                <el-option v-for="item in cases" :key="item.id" :label="item.name" :value="item.id" />
              </el-select>
            </template>
          </el-table-column>
          <el-table-column label="等待" width="116">
            <template #default="{ row }">
              <el-input-number
                v-model="row.settle_seconds"
                :min="0"
                :max="60"
                controls-position="right"
                class="settle-input"
              />
            </template>
          </el-table-column>
          <el-table-column label="必需文本" min-width="220">
            <template #default="{ row }"><el-input v-model="row.required_text" clearable /></template>
          </el-table-column>
          <el-table-column label="操作" width="76" align="center" fixed="right">
            <template #default="{ $index }">
              <el-button :icon="Delete" link type="danger" @click="removePageRow($index)" />
            </template>
          </el-table-column>
        </el-table>
      </section>
    </div>
  </div>
</template>

<style scoped>
.page-sets-page {
  flex: 1;
  height: 0;
  overflow: hidden;
  background: #f2f3f5;
}

.content-wrapper {
  height: calc(100% - 20px);
  margin: 10px;
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  gap: 12px;
}

.sets-panel,
.editor-panel {
  min-height: 0;
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 4px;
}

.sets-panel {
  display: flex;
  flex-direction: column;
}

.editor-panel {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.panel-header,
.header-actions,
.page-list-header {
  display: flex;
  align-items: center;
  gap: 12px;
}

.panel-header,
.page-list-header {
  justify-content: space-between;
}

.sets-panel .panel-header {
  padding: 14px;
  border-bottom: 1px solid #ebeef5;
}

.sets-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 8px;
}

.set-item {
  width: 100%;
  min-height: 54px;
  padding: 9px 10px;
  border: 1px solid transparent;
  border-radius: 4px;
  background: transparent;
  text-align: left;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 4px;
  cursor: pointer;
}

.set-item:hover {
  background: #f5f7fa;
}

.set-item.active {
  border-color: #409eff;
  background: #ecf5ff;
}

.set-name {
  font-weight: 600;
  color: #303133;
}

.card-title {
  font-size: 15px;
  font-weight: 700;
  color: #303133;
}

.muted-text {
  color: #909399;
  font-size: 12px;
}

.detail-title {
  margin-left: 8px;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 0 20px;
}

.pages-table {
  flex: 1;
  min-height: 0;
}

.pages-table :deep(.el-input),
.full-control,
.settle-input {
  width: 100%;
}

@media (max-width: 1180px) {
  .content-wrapper {
    grid-template-columns: 1fr;
  }

  .sets-panel {
    min-height: 220px;
  }
}
</style>
