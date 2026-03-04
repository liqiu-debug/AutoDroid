<script setup>
import { ref, computed, onMounted } from 'vue'
import { Search, Plus, FolderOpened, Document, Delete, Edit } from '@element-plus/icons-vue'
import { useCaseStore } from '@/stores/useCaseStore'
import { storeToRefs } from 'pinia'
import { ElMessageBox, ElMessage } from 'element-plus'
import api from '@/api'

const caseStore = useCaseStore()
const { caseList, loading, currentCase } = storeToRefs(caseStore)

const searchQuery = ref('')

const filteredCases = computed(() => {
  if (!searchQuery.value) return caseList.value
  return caseList.value.filter(c => 
    c.name.toLowerCase().includes(searchQuery.value.toLowerCase())
  )
})

onMounted(() => {
  caseStore.fetchCaseList()
})

const handleSelectCase = (caseItem) => {
  caseStore.loadCase(caseItem.id)
}

const handleNewCase = async () => {
  try {
    const { value: name } = await ElMessageBox.prompt('请输入用例名称', '新建用例', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      inputValidator: (val) => !!val.trim() || '名称不能为空'
    })
    
    if (name) {
      caseStore.newCase()
      currentCase.value.name = name
      await caseStore.saveCase()
    }
  } catch (err) {
    if (err !== 'cancel') {
        ElMessage.error('创建失败')
    }
  }
}



const handleRenameCase = async (caseItem, event) => {
  event.stopPropagation()
  try {
    const { value } = await ElMessageBox.prompt('请输入新的用例名称', '重命名', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      inputValue: caseItem.name,
      inputValidator: (val) => !!val.trim() || '名称不能为空'
    })
    
    if (value && value !== caseItem.name) {
      // Need to fetch full case data first because update requires full object
      const fullCase = (await api.getTestCase(caseItem.id)).data
      fullCase.name = value
      await api.updateTestCase(caseItem.id, fullCase)
      ElMessage.success('重命名成功')
      caseStore.fetchCaseList()
      // If current case is the one renamed, update store
      if (currentCase.value.id === caseItem.id) {
          currentCase.value.name = value
      }
    }
  } catch (err) {
    if (err !== 'cancel') {
        ElMessage.error('重命名失败')
    }
  }
}

const handleDeleteCase = async (caseItem, event) => {
  event.stopPropagation()
  try {
    await ElMessageBox.confirm('确定删除此用例?', '警告', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning'
    })
    await api.deleteTestCase(caseItem.id)
    ElMessage.success('用例删除成功')
    caseStore.fetchCaseList()
    if (currentCase.value.id === caseItem.id) {
      caseStore.newCase()
    }
  } catch {}
}
</script>

<template>
  <div class="case-explorer">
    <div class="explorer-header">
      <span class="title">用例管理</span>
      <div class="actions">
        <el-button type="primary" :icon="Plus" @click="handleNewCase">新建</el-button>
      </div>
    </div>
    
    <div class="search-box">
      <el-input
        v-model="searchQuery"
        placeholder="搜索用例..."
        :prefix-icon="Search"
        size="small"
        clearable
      />
    </div>
    
    <div class="case-list" v-loading="loading">
      <div
        v-for="item in filteredCases"
        :key="item.id"
        class="case-item"
        :class="{ active: currentCase.id === item.id }"
        @click="handleSelectCase(item)"
      >
        <el-icon class="case-icon"><Document /></el-icon>
        <span class="case-name">{{ item.name }}</span>
        <div class="item-actions">
           <el-button
            class="action-btn"
            :icon="Edit"
            size="small"
            text
            type="primary"
            @click="handleRenameCase(item, $event)"
          />
          <el-button
            class="action-btn"
            :icon="Delete"
            size="small"
            text
            type="danger"
            @click="handleDeleteCase(item, $event)"
          />
        </div>
      </div>
      
      <el-empty v-if="filteredCases.length === 0 && !loading" description="暂无用例" :image-size="60" />
    </div>
  </div>
</template>

<style scoped>
.case-explorer {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #fff;
  border-right: 1px solid #e4e7ed;
}

.explorer-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 20px;
  border-bottom: 1px solid #e4e7ed;
  height: 57px;
  box-sizing: border-box;
  background-color: #fafafa;
}

.title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

.actions {
  display: flex;
  gap: 8px;
}

.search-box {
  padding: 12px 16px;
}

.case-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.case-item {
  display: flex;
  align-items: center;
  padding: 10px 12px;
  margin-bottom: 4px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s ease;
  color: #606266;
}

.case-item:hover {
  background: #f5f7fa;
}

.case-item.active {
  background: #ecf5ff;
  color: #409eff;
}

.case-icon {
  margin-right: 10px;
  font-size: 16px;
  color: #909399;
}

.case-item.active .case-icon {
  color: #409eff;
}

.case-name {
  flex: 1;
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin-right: 6px;
}

.item-actions {
  display: flex;
  opacity: 0;
  transition: opacity 0.2s;
}

.case-item:hover .item-actions {
  opacity: 1;
}

.action-btn {
  padding: 2px;
  margin-left: 4px;
  font-size: 14px;
}
</style>
