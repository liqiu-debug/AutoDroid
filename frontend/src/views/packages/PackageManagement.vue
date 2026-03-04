<script setup>
import { ref, onMounted, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled, Download, Delete, Box, Cellphone } from '@element-plus/icons-vue'
import api from '@/api'

// ==================== 状态 ====================
const packages = ref([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(20)

// 安装弹窗
const installDialogVisible = ref(false)
const installLoading = ref(false)
const installTarget = ref(null) // 当前要安装的包
const selectedSerial = ref('')
const deviceList = ref([])
const deviceLoading = ref(false)

// 上传相关
const uploadHeaders = computed(() => ({
  Authorization: `Bearer ${localStorage.getItem('token') || ''}`
}))
const uploadAction = '/api/packages/upload'

// ==================== 方法 ====================

/** 加载安装包列表 */
const fetchPackages = async () => {
  loading.value = true
  try {
    const { data } = await api.getPackages({
      page: currentPage.value,
      page_size: pageSize.value
    })
    packages.value = data.items || []
    total.value = data.total || 0
  } catch (e) {
    ElMessage.error('加载失败：' + (e.response?.data?.detail || e.message))
  } finally {
    loading.value = false
  }
}

/** 上传成功回调 */
const handleUploadSuccess = (response) => {
  ElMessage.success(`上传成功：${response.app_name} v${response.version_name}`)
  fetchPackages()
}

/** 上传失败回调 */
const handleUploadError = (error) => {
  let msg = '上传失败'
  try {
    const parsed = JSON.parse(error.message)
    msg = parsed.detail || msg
  } catch { /* ignore */ }
  ElMessage.error(msg)
}

/** 上传前校验 */
const beforeUpload = (file) => {
  const isAPK = file.name.toLowerCase().endsWith('.apk')
  if (!isAPK) {
    ElMessage.warning('仅支持 .apk 文件')
  }
  return isAPK
}

/** 下载安装包 */
const handleDownload = (row) => {
  const url = api.getPackageDownloadUrl(row.id)
  const token = localStorage.getItem('token')
  // 创建一个临时 a 标签触发下载
  const link = document.createElement('a')
  link.href = `${url}?token=${token}`
  link.download = `${row.app_name}_${row.version_name}.apk`
  link.target = '_blank'
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
}

/** 删除安装包 */
const handleDelete = async (row) => {
  try {
    await ElMessageBox.confirm(
      `确定要删除 ${row.app_name} v${row.version_name} 吗？`,
      '确认删除',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
    await api.deletePackage(row.id)
    ElMessage.success('删除成功')
    fetchPackages()
  } catch (e) {
    if (e !== 'cancel') {
      ElMessage.error('删除失败：' + (e.response?.data?.detail || e.message))
    }
  }
}

/** 打开安装弹窗 */
const openInstallDialog = async (row) => {
  installTarget.value = row
  selectedSerial.value = ''
  installDialogVisible.value = true
  // 加载设备列表
  deviceLoading.value = true
  try {
    const { data } = await api.getDeviceList()
    deviceList.value = data || []
  } catch (e) {
    ElMessage.error('获取设备列表失败')
    deviceList.value = []
  } finally {
    deviceLoading.value = false
  }
}

/** 确认安装 */
const handleInstall = async () => {
  if (!selectedSerial.value) {
    ElMessage.warning('请选择目标设备')
    return
  }
  installLoading.value = true
  try {
    const { data } = await api.installPackage(installTarget.value.id, selectedSerial.value)
    ElMessage.success(data.msg || '安装成功')
    installDialogVisible.value = false
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '安装失败')
  } finally {
    installLoading.value = false
  }
}

/** 文件大小格式化 */
const formatSize = (size) => {
  if (!size) return '—'
  return size >= 1 ? `${size.toFixed(1)} MB` : `${(size * 1024).toFixed(0)} KB`
}

/** 时间格式化 */
const formatTime = (time) => {
  if (!time) return '—'
  const d = new Date(time)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 分页切换 */
const handlePageChange = (page) => {
  currentPage.value = page
  fetchPackages()
}

// ==================== 生命周期 ====================
onMounted(() => {
  fetchPackages()
})
</script>

<template>
  <div class="package-management">

    <!-- 顶部工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <el-icon :size="22" color="#409eff"><Box /></el-icon>
        <h2 class="page-title">App包管理</h2>
        <el-tag type="info" size="small" style="margin-left: 12px;">
          {{ total }} 个安装包
        </el-tag>
      </div>
    </div>

    <!-- 拖拽上传区 -->
    <el-card class="upload-card" shadow="never">
      <el-upload
        :action="uploadAction"
        :headers="uploadHeaders"
        :on-success="handleUploadSuccess"
        :on-error="handleUploadError"
        :before-upload="beforeUpload"
        :show-file-list="false"
        accept=".apk"
        drag
        name="file"
      >
        <el-icon class="upload-icon"><UploadFilled /></el-icon>
        <div class="upload-text">将 APK 文件拖到此处，或 <em>点击上传</em></div>
        <div class="upload-tip">仅支持 .apk 格式文件</div>
      </el-upload>
    </el-card>

    <!-- 版本库表格 -->
    <el-card class="table-card" shadow="never">
      <el-table
        :data="packages"
        v-loading="loading"
        stripe
        style="width: 100%;"
        empty-text="暂无安装包，请上传 APK 文件"
      >
        <!-- 应用名称 + 最新标签 -->
        <el-table-column label="应用名称" min-width="180">
          <template #default="{ row }">
            <div class="app-name-cell">
              <span class="app-name">{{ row.app_name }}</span>
              <el-tag
                v-if="row.is_latest"
                type="success"
                size="small"
                effect="dark"
                round
              >最新</el-tag>
            </div>
          </template>
        </el-table-column>

        <!-- 包名 -->
        <el-table-column label="包名" prop="package_name" min-width="220">
          <template #default="{ row }">
            <span class="mono-text">{{ row.package_name || '—' }}</span>
          </template>
        </el-table-column>

        <!-- 版本号 -->
        <el-table-column label="版本号" min-width="140">
          <template #default="{ row }">
            <span>{{ row.version_name || '—' }}</span>
            <span v-if="row.version_code" class="version-code">({{ row.version_code }})</span>
          </template>
        </el-table-column>

        <!-- 文件大小 -->
        <el-table-column label="文件大小" width="120" align="center">
          <template #default="{ row }">
            {{ formatSize(row.file_size) }}
          </template>
        </el-table-column>

        <!-- 上传时间 -->
        <el-table-column label="上传时间" width="170" align="center">
          <template #default="{ row }">
            {{ formatTime(row.upload_time) }}
          </template>
        </el-table-column>

        <!-- 上传者 -->
        <el-table-column label="上传者" prop="uploader_name" width="100" align="center" />

        <!-- 操作 -->
        <el-table-column label="操作" width="220" align="center" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link :icon="Cellphone" @click="openInstallDialog(row)">
              安装
            </el-button>
            <el-button type="primary" link :icon="Download" @click="handleDownload(row)">
              下载
            </el-button>
            <el-button type="danger" link :icon="Delete" @click="handleDelete(row)">
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
      <div class="pagination-wrapper" v-if="total > pageSize">
        <el-pagination
          v-model:current-page="currentPage"
          :page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next"
          @current-change="handlePageChange"
        />
      </div>
    </el-card>

    <!-- 安装到设备弹窗 -->
    <el-dialog
      v-model="installDialogVisible"
      title="推送到指定设备"
      width="480px"
      align-center
      destroy-on-close
    >
      <div v-if="installTarget" style="margin-bottom: 16px; color: #606266;">
        即将安装：<strong>{{ installTarget.app_name }}</strong> v{{ installTarget.version_name }}
      </div>
      <el-form label-width="80px">
        <el-form-item label="目标设备">
          <el-select
            v-model="selectedSerial"
            placeholder="请选择设备"
            style="width: 100%;"
            v-loading="deviceLoading"
          >
            <el-option
              v-for="d in deviceList"
              :key="d.serial"
              :value="d.serial"
              :label="(d.custom_name || d.model) + ' (' + d.serial + ')'"
              :disabled="d.status !== 'IDLE'"
            >
              <span>{{ d.custom_name || d.model }}</span>
              <span style="float: right; color: #909399; font-size: 12px;">
                {{ d.status === 'IDLE' ? '🟢 空闲' : d.status === 'BUSY' ? '🔴 运行中' : '⚫ 离线' }}
              </span>
            </el-option>
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="installDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="installLoading" @click="handleInstall">
          确定安装
        </el-button>
      </template>
    </el-dialog>

  </div>
</template>

<style scoped>
.package-management {
  padding: 20px 24px;
  height: 100%;
  overflow-y: auto;
  background: linear-gradient(135deg, #f5f7fa 0%, #e4e7ed 100%);
}

/* 工具栏 */
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  padding: 16px 20px;
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.06);
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.page-title {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  color: #303133;
}

/* 上传区 */
.upload-card {
  margin-bottom: 20px;
  border-radius: 12px;
  border: none;
}

.upload-card :deep(.el-upload) {
  width: 100%;
}

.upload-card :deep(.el-upload-dragger) {
  width: 100%;
  padding: 40px 20px;
  border: 2px dashed #dcdfe6;
  border-radius: 12px;
  background: linear-gradient(135deg, #f0f9ff 0%, #e8f4fd 100%);
  transition: all 0.3s ease;
}

.upload-card :deep(.el-upload-dragger:hover) {
  border-color: #409eff;
  background: linear-gradient(135deg, #e6f3ff 0%, #d4edff 100%);
}

.upload-icon {
  font-size: 52px;
  color: #409eff;
  margin-bottom: 12px;
}

.upload-text {
  font-size: 15px;
  color: #606266;
}

.upload-text em {
  color: #409eff;
  font-style: normal;
  font-weight: 600;
}

.upload-tip {
  font-size: 12px;
  color: #909399;
  margin-top: 8px;
}

/* 表格区 */
.table-card {
  border-radius: 12px;
  border: none;
}

.app-name-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}

.app-name {
  font-weight: 600;
  color: #303133;
}

.mono-text {
  font-family: 'SF Mono', 'Menlo', 'Monaco', monospace;
  font-size: 12px;
  color: #606266;
}

.version-code {
  font-size: 12px;
  color: #909399;
  margin-left: 4px;
}

.pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  padding: 16px 0 4px;
}
</style>
