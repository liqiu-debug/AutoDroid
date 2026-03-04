<script setup>
import { ref, nextTick, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Picture, Unlock, SwitchButton, Monitor, Edit } from '@element-plus/icons-vue'
import api from '@/api'

// ==================== 状态 ====================
const devices = ref([])
const loading = ref(false)
const syncLoading = ref(false)

// 快照弹窗
const screenshotVisible = ref(false)
const screenshotLoading = ref(false)
const screenshotData = ref('')
const screenshotDevice = ref(null)

// 就地编辑
const editingSerial = ref(null)
const editingName = ref('')

// ==================== 方法 ====================

/** 获取当前设备列表 */
const fetchDevices = async () => {
  loading.value = true
  try {
    const { data } = await api.getDeviceList()
    devices.value = data
  } catch (e) {
    console.error(e)
  } finally {
    loading.value = false
  }
}

/** 一键同步物理设备 */
const handleSync = async () => {
  syncLoading.value = true
  try {
    const { data } = await api.syncDevices()
    devices.value = data.devices || []
    ElMessage.success(`同步完成：${data.online} 台在线，${data.offline} 台离线`)
  } catch (e) {
    ElMessage.error('同步失败：' + (e.response?.data?.detail || e.message))
  } finally {
    syncLoading.value = false
  }
}

/** 打开快照弹窗 */
const handleScreenshot = async (device) => {
  screenshotDevice.value = device
  screenshotData.value = ''
  screenshotVisible.value = true
  await refreshScreenshot(device.serial)
}

/** 刷新快照 */
const refreshScreenshot = async (serial) => {
  screenshotLoading.value = true
  try {
    const { data } = await api.getDeviceScreenshot(serial)
    screenshotData.value = data.base64_img
  } catch (e) {
    ElMessage.error('截图失败：' + (e.response?.data?.detail || e.message))
  } finally {
    screenshotLoading.value = false
  }
}

/** 强制释放锁 */
const handleUnlock = async (device) => {
  try {
    await api.unlockDevice(device.serial)
    ElMessage.success(`设备 ${device.model} 已释放`)
    await fetchDevices()
  } catch (e) {
    ElMessage.error('释放失败：' + (e.response?.data?.detail || e.message))
  }
}

/** 重启设备 */
const handleReboot = async (device) => {
  try {
    await ElMessageBox.confirm(
      `确定要重启设备 ${device.model} (${device.serial}) 吗？设备将暂时离线。`,
      '确认重启',
      { type: 'warning', confirmButtonText: '确定重启', cancelButtonText: '取消' }
    )
    await api.rebootDevice(device.serial)
    ElMessage.success(`设备 ${device.model} 正在重启`)
    await fetchDevices()
  } catch (e) {
    if (e !== 'cancel') {
      ElMessage.error('重启失败：' + (e.response?.data?.detail || e.message))
    }
  }
}

/** 进入就地编辑模式 */
const startEditing = (device) => {
  editingSerial.value = device.serial
  editingName.value = device.custom_name || device.market_name || device.model
  nextTick(() => {
    // 自动聚焦：通过 ref 或 DOM 查询
    const input = document.querySelector('.inline-edit-input input')
    if (input) input.focus()
  })
}

/** 保存就地编辑 */
const saveEditing = async (device) => {
  const newName = editingName.value.trim()
  editingSerial.value = null
  // 空值或与 model 一致时，清空 custom_name（优先使用 market_name 对比）
  const displayName = device.market_name || device.model
  const finalName = (!newName || newName === displayName) ? '' : newName
  if (finalName === (device.custom_name || '')) return
  try {
    await api.renameDevice(device.serial, finalName)
    device.custom_name = finalName || null
    ElMessage.success(finalName ? '设备名称已更新' : '已恢复默认名称')
  } catch (e) {
    ElMessage.error('修改失败：' + (e.response?.data?.detail || e.message))
  }
}

/** 取消编辑 */
const cancelEditing = () => {
  editingSerial.value = null
}

/** 状态标签类型映射 */
const statusTagType = (status) => {
  const map = { IDLE: 'success', BUSY: 'danger', OFFLINE: 'info' }
  return map[status] || 'info'
}

/** 状态中文映射 */
const statusLabel = (status) => {
  const map = { IDLE: '🟢 空闲', BUSY: '🔴 执行中', OFFLINE: '⚫ 离线' }
  return map[status] || status
}

// ==================== 生命周期 ====================
onMounted(() => {
  handleSync()
})
</script>

<template>
  <div class="device-center" v-loading.fullscreen.lock="syncLoading" element-loading-text="正在同步设备，请稍候...">

    <!-- 顶部工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <el-icon :size="22" color="#409eff"><Monitor /></el-icon>
        <h2 class="page-title">设备管理中心</h2>
        <el-tag type="info" size="small" style="margin-left: 12px;">
          {{ devices.length }} 台设备
        </el-tag>
      </div>
      <el-button type="primary" :icon="Refresh" @click="handleSync" :loading="syncLoading">
        一键同步物理设备
      </el-button>
    </div>

    <!-- 空状态 -->
    <el-empty
      v-if="!loading && devices.length === 0"
      description="暂无设备，请点击「一键同步物理设备」按钮"
      :image-size="120"
    />

    <!-- 设备卡片网格 -->
    <el-row :gutter="20" v-else>
      <el-col :xs="24" :sm="12" :md="8" :lg="6" v-for="device in devices" :key="device.serial">
        <el-card class="device-card" shadow="hover">
          <!-- Header -->
          <template #header>
            <div class="card-header">
              <div class="card-header-left">
                <!-- 就地编辑模式 -->
                <div v-if="editingSerial === device.serial" class="inline-edit-wrapper">
                  <el-input
                    v-model="editingName"
                    size="small"
                    class="inline-edit-input"
                    placeholder="输入设备名称"
                    @blur="saveEditing(device)"
                    @keyup.enter="saveEditing(device)"
                    @keyup.escape="cancelEditing"
                  />
                </div>
                <!-- 展示模式 -->
                <div v-else class="device-name-display" @click="startEditing(device)">
                  <span class="device-title">{{ device.custom_name || device.market_name || device.model }}</span>
                  <el-icon class="edit-icon" :size="13"><Edit /></el-icon>
                </div>
                <!-- 用户修改了名称时，显示 market_name；否则显示 model（如果不同于 market_name） -->
                <span v-if="device.custom_name && device.market_name && device.market_name !== device.custom_name" class="device-model-sub">{{ device.market_name }}</span>
                <span v-else-if="!device.custom_name && device.market_name && device.market_name !== device.model" class="device-model-sub">{{ device.model }}</span>
              </div>
              <el-tag :type="statusTagType(device.status)" size="small" effect="dark" round>
                {{ statusLabel(device.status) }}
              </el-tag>
            </div>
          </template>

          <!-- Body -->
          <div class="card-body">
            <div class="info-row">
              <span class="info-label">手机厂商</span>
              <span class="info-value">{{ device.brand || '—' }}</span>
            </div>
            <div class="info-row">
              <span class="info-label">系统版本</span>
              <span class="info-value">Android {{ device.android_version || '—' }}</span>
            </div>
            <div class="info-row">
              <span class="info-label">设备编号</span>
              <span class="info-value serial">{{ device.serial }}</span>
            </div>
            <div class="info-row">
              <span class="info-label">屏幕分辨率</span>
              <span class="info-value">{{ device.resolution || '—' }}</span>
            </div>
          </div>

          <!-- Footer -->
          <div class="card-footer">
            <el-button type="primary" link :icon="Picture" @click="handleScreenshot(device)"
              :disabled="device.status === 'OFFLINE'">
              快照
            </el-button>
            <el-popconfirm
              title="确定要释放该设备锁吗？"
              confirm-button-text="释放"
              cancel-button-text="取消"
              @confirm="handleUnlock(device)"
            >
              <template #reference>
                <el-button type="danger" link :icon="Unlock"
                  :disabled="device.status === 'OFFLINE'">
                  释放锁
                </el-button>
              </template>
            </el-popconfirm>
            <el-button type="warning" link :icon="SwitchButton" @click="handleReboot(device)"
              :disabled="device.status === 'OFFLINE'">
              重启
            </el-button>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 快照弹窗 -->
    <el-dialog
      v-model="screenshotVisible"
      :title="`实时屏幕快照 — ${screenshotDevice?.model || ''}`"
      width="420px"
      align-center
      destroy-on-close
    >
      <div class="screenshot-container" v-loading="screenshotLoading">
        <img
          v-if="screenshotData"
          :src="`data:image/png;base64,${screenshotData}`"
          class="screenshot-img"
          alt="设备截图"
        />
        <el-empty v-else description="暂无截图" :image-size="80" />
      </div>
      <template #footer>
        <el-button :icon="Refresh" @click="refreshScreenshot(screenshotDevice?.serial)" :loading="screenshotLoading">
          刷新屏幕
        </el-button>
      </template>
    </el-dialog>

  </div>
</template>

<style scoped>
.device-center {
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
  margin-bottom: 24px;
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

/* 设备卡片 */
.device-card {
  margin-bottom: 20px;
  border-radius: 12px;
  transition: transform 0.25s ease, box-shadow 0.25s ease;
  overflow: hidden;
}

.device-card:hover {
  transform: translateY(-4px);
  box-shadow: 0 8px 24px rgba(64, 158, 255, 0.15);
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.card-header-left {
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow: hidden;
  flex: 1;
  min-width: 0;
}

/* 展示模式 */
.device-name-display {
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  border-radius: 4px;
  padding: 2px 4px;
  margin: -2px -4px;
  transition: background-color 0.2s;
}

.device-name-display:hover {
  background-color: #f5f7fa;
}

.device-name-display:hover .edit-icon {
  opacity: 1;
}

.device-title {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.edit-icon {
  opacity: 0;
  color: #909399;
  flex-shrink: 0;
  transition: opacity 0.2s, color 0.2s;
}

.edit-icon:hover {
  color: #409eff;
}

.device-model-sub {
  font-size: 11px;
  color: #909399;
  padding-left: 4px;
}

/* 编辑模式 */
.inline-edit-wrapper {
  width: 100%;
}

.inline-edit-input {
  width: 100%;
}

/* 卡片 Body */
.card-body {
  padding: 4px 0 8px;
}

.info-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 6px 0;
  border-bottom: 1px dashed #ebeef5;
}

.info-row:last-child {
  border-bottom: none;
}

.info-label {
  font-size: 12px;
  color: #909399;
  flex-shrink: 0;
}

.info-value {
  font-size: 13px;
  color: #303133;
  font-weight: 500;
  text-align: right;
}

.info-value.serial {
  font-family: 'SF Mono', 'Menlo', 'Monaco', monospace;
  font-size: 11px;
  color: #606266;
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 卡片 Footer */
.card-footer {
  display: flex;
  justify-content: space-around;
  padding-top: 12px;
  border-top: 1px solid #ebeef5;
}

/* 快照弹窗 */
.screenshot-container {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 300px;
  background: #1a1a2e;
  border-radius: 8px;
  overflow: hidden;
}

.screenshot-img {
  max-width: 100%;
  max-height: 560px;
  object-fit: contain;
  border-radius: 4px;
}
</style>
