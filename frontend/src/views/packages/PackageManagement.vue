<script setup>
import { ref, onMounted, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled, Download, Delete, Box, Cellphone } from '@element-plus/icons-vue'
import api from '@/api'

const CHUNK_SIZE = 20 * 1024 * 1024
const MAX_CHUNK_RETRIES = 2

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
const uploadDialogVisible = ref(false)
const uploadInProgress = ref(false)
const uploadCancelling = ref(false)
const uploadStatus = ref('idle')
const uploadStatusText = ref('')
const uploadFileName = ref('')
const uploadFileSize = ref(0)
const uploadUploadedBytes = ref(0)
const uploadCurrentChunkLoaded = ref(0)
const uploadCurrentChunk = ref(0)
const uploadTotalChunks = ref(0)
const activeUploadId = ref('')

let uploadCancelRequested = false
let activeUploadController = null

const uploadProgress = computed(() => {
  if (!uploadFileSize.value) return 0
  const uploaded = uploadUploadedBytes.value + uploadCurrentChunkLoaded.value
  return Math.min(100, Math.max(0, Math.round((uploaded / uploadFileSize.value) * 100)))
})

const uploadProgressStatus = computed(() => {
  if (uploadStatus.value === 'error') return 'exception'
  if (uploadStatus.value === 'success') return 'success'
  return undefined
})

const uploadDialogCanClose = computed(() => !uploadInProgress.value && !uploadCancelling.value)
const uploadChunkLabel = computed(() => (
  uploadTotalChunks.value ? `${uploadCurrentChunk.value} / ${uploadTotalChunks.value}` : '—'
))

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

/** 上传前校验 */
const beforeUpload = (file) => {
  if (uploadInProgress.value) {
    ElMessage.warning('已有上传任务进行中')
    return false
  }
  const extension = file.name.toLowerCase().split('.').pop()
  const supported = extension === 'apk' || extension === 'ipa'
  if (!supported) {
    ElMessage.warning('仅支持 .apk 或 .ipa 文件')
  }
  return supported
}

const getErrorMessage = (error, fallback = '上传失败') => (
  error?.response?.data?.detail || error?.message || fallback
)

const isCancelError = (error) => (
  uploadCancelRequested || error?.code === 'ERR_CANCELED' || error?.name === 'CanceledError'
)

const resetUploadProgress = () => {
  uploadStatus.value = 'idle'
  uploadStatusText.value = ''
  uploadFileName.value = ''
  uploadFileSize.value = 0
  uploadUploadedBytes.value = 0
  uploadCurrentChunkLoaded.value = 0
  uploadCurrentChunk.value = 0
  uploadTotalChunks.value = 0
  activeUploadId.value = ''
  uploadCancelRequested = false
  activeUploadController = null
}

const formatBytes = (bytes) => {
  if (!bytes) return '0 KB'
  const mb = bytes / (1024 * 1024)
  if (mb >= 1) return `${mb >= 100 ? mb.toFixed(0) : mb.toFixed(1)} MB`
  return `${(bytes / 1024).toFixed(0)} KB`
}

const cleanupActiveUploadSession = async () => {
  if (!activeUploadId.value) return
  try {
    await api.cancelPackageUpload(activeUploadId.value)
  } catch (error) {
    if (!error?.response || error.response.status !== 404) {
      ElMessage.warning(getErrorMessage(error, '取消上传失败，临时文件会在 24 小时后自动清理'))
    }
  }
}

const cancelActiveUpload = async () => {
  uploadCancelRequested = true
  uploadCancelling.value = true
  uploadStatusText.value = '正在取消上传'
  if (activeUploadController) {
    activeUploadController.abort()
  }
  await cleanupActiveUploadSession()
}

const uploadChunkWithRetry = async (uploadId, index, chunk, fileName) => {
  let attempt = 0
  while (attempt <= MAX_CHUNK_RETRIES) {
    if (uploadCancelRequested) {
      throw new Error('上传已取消')
    }

    const formData = new FormData()
    formData.append('file', chunk, fileName)
    activeUploadController = new AbortController()

    try {
      await api.uploadPackageChunk(uploadId, index, formData, {
        signal: activeUploadController.signal,
        onUploadProgress: (event) => {
          uploadCurrentChunkLoaded.value = Math.min(event.loaded || 0, chunk.size)
        }
      })
      return
    } catch (error) {
      if (isCancelError(error)) {
        throw error
      }
      if (attempt >= MAX_CHUNK_RETRIES) {
        throw error
      }
      attempt += 1
      uploadCurrentChunkLoaded.value = 0
      uploadStatusText.value = `分片 ${index + 1} 上传失败，正在重试 ${attempt} / ${MAX_CHUNK_RETRIES}`
    } finally {
      activeUploadController = null
    }
  }
}

const uploadPackageInChunks = async ({ file, onSuccess, onError }) => {
  resetUploadProgress()
  uploadDialogVisible.value = true
  uploadInProgress.value = true
  uploadStatus.value = 'uploading'
  uploadFileName.value = file.name
  uploadFileSize.value = file.size
  uploadTotalChunks.value = Math.ceil(file.size / CHUNK_SIZE)
  uploadStatusText.value = '正在创建上传会话'

  try {
    const { data: session } = await api.createPackageUploadSession({
      filename: file.name,
      file_size: file.size,
      chunk_size: CHUNK_SIZE,
      total_chunks: uploadTotalChunks.value
    })
    activeUploadId.value = session.upload_id

    for (let index = 0; index < uploadTotalChunks.value; index += 1) {
      if (uploadCancelRequested) {
        throw new Error('上传已取消')
      }
      const start = index * CHUNK_SIZE
      const end = Math.min(file.size, start + CHUNK_SIZE)
      const chunk = file.slice(start, end)
      uploadCurrentChunk.value = index + 1
      uploadCurrentChunkLoaded.value = 0
      uploadStatusText.value = `正在上传分片 ${index + 1} / ${uploadTotalChunks.value}`

      await uploadChunkWithRetry(session.upload_id, index, chunk, file.name)
      uploadUploadedBytes.value += chunk.size
      uploadCurrentChunkLoaded.value = 0
    }

    uploadStatus.value = 'merging'
    uploadStatusText.value = '正在合并并解析安装包'
    const { data: result } = await api.completePackageUpload(session.upload_id)

    uploadStatus.value = 'success'
    uploadUploadedBytes.value = file.size
    uploadStatusText.value = '上传完成'
    ElMessage.success(`上传成功：${result.app_name} v${result.version_name}`)
    onSuccess?.(result)
    await fetchPackages()
    uploadDialogVisible.value = false
  } catch (error) {
    if (isCancelError(error)) {
      await cleanupActiveUploadSession()
      uploadStatus.value = 'cancelled'
      uploadStatusText.value = '上传已取消'
      ElMessage.info('上传已取消')
    } else {
      uploadStatus.value = 'error'
      uploadStatusText.value = getErrorMessage(error)
      ElMessage.error(uploadStatusText.value)
    }
    onError?.(error)
  } finally {
    uploadInProgress.value = false
    uploadCancelling.value = false
    activeUploadController = null
  }
}

/** 下载安装包 */
const handleDownload = (row) => {
  const url = api.getPackageDownloadUrl(row.id)
  const token = localStorage.getItem('token')
  // 通过动态 a 标签触发浏览器下载
  const link = document.createElement('a')
  link.href = `${url}?token=${token}`
  const extension = row.platform === 'ios' ? 'ipa' : 'apk'
  link.download = `${row.app_name}_${row.version_name}.${extension}`
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
    const targetPlatform = row.platform || 'android'
    deviceList.value = (data || []).filter(d => (d.platform || 'android') === targetPlatform)
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

/** 设备名称展示 */
const getDeviceDisplayName = (device) => (
  device?.custom_name || device?.market_name || device?.model || device?.serial || ''
)

const isDeviceInstallable = (device) => {
  if (!device) return false
  if ((installTarget.value?.platform || 'android') === 'ios') {
    return device.status === 'IDLE' || device.status === 'WDA_DOWN'
  }
  return device.status === 'IDLE'
}

const getDeviceStatusText = (device) => {
  if (device.status === 'IDLE') return '🟢 空闲'
  if (device.status === 'BUSY') return '🔴 运行中'
  if (device.status === 'WDA_DOWN') return '🟠 WDA异常（可安装）'
  return '⚫ 离线'
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
        action="/api/packages/upload-sessions"
        :http-request="uploadPackageInChunks"
        :before-upload="beforeUpload"
        :show-file-list="false"
        :disabled="uploadInProgress"
        accept=".apk,.ipa"
        drag
        name="file"
      >
        <el-icon class="upload-icon"><UploadFilled /></el-icon>
        <div class="upload-text">将 APK 或 IPA 文件拖到此处，或 <em>点击上传</em></div>
        <div class="upload-tip">支持 Android .apk 与 Ad Hoc 签名的 iOS .ipa</div>
      </el-upload>
    </el-card>

    <el-dialog
      v-model="uploadDialogVisible"
      title="安装包上传"
      width="460px"
      :show-close="uploadDialogCanClose"
      :close-on-click-modal="uploadDialogCanClose"
      :close-on-press-escape="uploadDialogCanClose"
      @closed="resetUploadProgress"
    >
      <div class="upload-progress-dialog">
        <div class="upload-file-name">{{ uploadFileName || '—' }}</div>
        <div class="upload-meta-grid">
          <span>大小</span>
          <strong>{{ formatBytes(uploadFileSize) }}</strong>
          <span>分片</span>
          <strong>{{ uploadChunkLabel }}</strong>
        </div>
        <el-progress
          :percentage="uploadProgress"
          :status="uploadProgressStatus"
          :stroke-width="10"
          striped
          striped-flow
        />
        <div class="upload-status-text">{{ uploadStatusText }}</div>
      </div>
      <template #footer>
        <el-button
          v-if="uploadInProgress && uploadStatus === 'uploading'"
          :loading="uploadCancelling"
          @click="cancelActiveUpload"
        >
          取消上传
        </el-button>
        <el-button v-else :disabled="!uploadDialogCanClose" @click="uploadDialogVisible = false">
          关闭
        </el-button>
      </template>
    </el-dialog>

    <!-- 版本库表格 -->
    <el-card class="table-card" shadow="never">
      <el-table
        :data="packages"
        v-loading="loading"
        stripe
        style="width: 100%;"
        empty-text="暂无安装包，请上传 APK 或 IPA 文件"
      >
        <!-- 应用名称 + 最新标签 -->
        <el-table-column label="应用名称" min-width="180">
          <template #default="{ row }">
            <div class="app-name-cell">
              <span class="app-name">{{ row.app_name }}</span>
              <el-tag
                :type="row.platform === 'ios' ? 'primary' : 'info'"
                size="small"
                effect="plain"
                round
              >{{ row.platform === 'ios' ? 'iOS' : 'Android' }}</el-tag>
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
        <el-table-column label="包名 / Bundle ID" prop="package_name" min-width="220">
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
      :title="installTarget?.platform === 'ios' ? '安装 IPA 到指定 iPhone' : '推送到指定设备'"
      width="480px"
      align-center
      destroy-on-close
    >
      <div v-if="installTarget" style="margin-bottom: 16px; color: #606266;">
        即将安装：<strong>{{ installTarget.app_name }}</strong> v{{ installTarget.version_name }}
      </div>
      <el-alert
        v-if="installTarget?.platform === 'ios'"
        title="目标 iPhone 需已配对并信任当前 Mac、开启开发者模式，且 UDID 已包含在 Ad Hoc 描述文件中。"
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 16px;"
      />
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
              :label="getDeviceDisplayName(d)"
              :disabled="!isDeviceInstallable(d)"
            >
              <span>{{ getDeviceDisplayName(d) }}</span>
              <span style="float: right; color: #909399; font-size: 12px;">
                {{ getDeviceStatusText(d) }}
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

.upload-progress-dialog {
  display: grid;
  gap: 16px;
}

.upload-file-name {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
  word-break: break-all;
}

.upload-meta-grid {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 8px 14px;
  font-size: 13px;
}

.upload-meta-grid span {
  color: #909399;
}

.upload-meta-grid strong {
  color: #303133;
  font-weight: 600;
}

.upload-status-text {
  min-height: 20px;
  font-size: 13px;
  color: #606266;
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
