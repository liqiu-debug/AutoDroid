<script setup>
import { ref, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Picture, Unlock, SwitchButton, Monitor, Edit, Delete, CircleClose } from '@element-plus/icons-vue'
import api from '@/api'
import { useClientMode } from '@/composables/useClientMode'

const { isMobileMode } = useClientMode()

// ==================== 状态 ====================
const devices = ref([])
const loading = ref(false)
const syncLoading = ref(false)
const wdaCheckingSerial = ref('')
const deleteLoadingSerial = ref('')
const stopLoadingSerial = ref('')

// 快照弹窗
const screenshotVisible = ref(false)
const screenshotLoading = ref(false)
const screenshotData = ref('')
const screenshotDevice = ref(null)

// 就地编辑
const editingSerial = ref(null)
const editingName = ref('')
const autoRefreshTimer = ref(null)
const autoRefreshing = ref(false)
const DEVICE_LIST_REFRESH_INTERVAL_MS = 5000

// ==================== 方法 ====================

/** 获取当前设备列表 */
const fetchDevices = async ({ refreshIosWda = false, silent = false } = {}) => {
  if (autoRefreshing.value) return
  autoRefreshing.value = true
  if (!silent) {
    loading.value = true
  }
  try {
    const { data } = await api.getDeviceList({ refreshIosWda })
    devices.value = data
  } catch (e) {
    console.error(e)
  } finally {
    autoRefreshing.value = false
    if (!silent) {
      loading.value = false
    }
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

/** 停止当前设备上的执行 */
const handleStopExecution = async (device) => {
  try {
    await ElMessageBox.confirm(
      `确定要停止 ${device.custom_name || device.market_name || device.model || device.serial} 当前执行吗？`,
      '停止当前设备执行',
      { type: 'warning', confirmButtonText: '停止执行', cancelButtonText: '取消' }
    )
    stopLoadingSerial.value = device.serial
    const { data } = await api.stopDeviceExecution(device.serial)
    const count = Number(data?.recovered_executions || 0)
    ElMessage.success(count > 0 ? `已停止 ${count} 条运行中执行` : '已发送停止指令')
    await fetchDevices({ refreshIosWda: true })
  } catch (e) {
    if (!['cancel', 'close'].includes(e)) {
      ElMessage.error('停止失败：' + (e.response?.data?.detail || e.message))
    }
  } finally {
    stopLoadingSerial.value = ''
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

const isDeviceOffline = (device) => String(device?.status || '').trim().toUpperCase() === 'OFFLINE'

/** 删除离线设备 */
const handleDeleteDevice = async (device) => {
  if (!isDeviceOffline(device)) return
  try {
    await ElMessageBox.confirm(
      `确定要删除设备 ${device.model} (${device.serial}) 的信息吗？删除后可通过同步物理设备重新发现。`,
      '确认删除设备',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' }
    )
    deleteLoadingSerial.value = device.serial
    await api.deleteDevice(device.serial)
    devices.value = devices.value.filter(item => item.serial !== device.serial)
    ElMessage.success('设备信息已删除')
  } catch (e) {
    if (!['cancel', 'close'].includes(e)) {
      ElMessage.error('删除失败：' + (e.response?.data?.detail || e.message))
    }
  } finally {
    deleteLoadingSerial.value = ''
  }
}

/** iOS WDA 启动/修复 */
const handleCheckWda = async (device) => {
  if (!device || device.platform !== 'ios') return
  wdaCheckingSerial.value = device.serial
  try {
    const { data } = await api.checkDeviceWda(device.serial)
    device.status = data.status || device.status
    if (data.wda_healthy) {
      if (data.attempted_start) {
        ElMessage.success(`设备 ${device.model} WDA 启动成功`)
      } else if (data.recovered_by_cleanup) {
        ElMessage.success(`设备 ${device.model} WDA 已修复`)
      } else {
        ElMessage.success(`设备 ${device.model} WDA 正常`)
      }
    } else {
      ElMessage.warning(data.error || 'WDA 启动失败，请检查 WebDriverAgent 与 tidevice 环境')
    }
  } catch (e) {
    ElMessage.error('WDA 启动失败：' + (e.response?.data?.detail || e.message))
  } finally {
    wdaCheckingSerial.value = ''
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

const startAutoRefresh = () => {
  if (autoRefreshTimer.value) return
  autoRefreshTimer.value = setInterval(() => {
    fetchDevices({ refreshIosWda: true, silent: true })
  }, DEVICE_LIST_REFRESH_INTERVAL_MS)
}

const stopAutoRefresh = () => {
  if (!autoRefreshTimer.value) return
  clearInterval(autoRefreshTimer.value)
  autoRefreshTimer.value = null
}

/** 状态标签类型映射 */
const statusTagType = (status) => {
  const map = { IDLE: 'success', BUSY: 'danger', OFFLINE: 'info', WDA_DOWN: 'warning' }
  return map[status] || 'info'
}

/** 状态中文映射 */
const statusLabel = (status) => {
  const map = { IDLE: '🟢 空闲', BUSY: '🔴 执行中', OFFLINE: '⚫ 离线', WDA_DOWN: '🟠 WDA异常' }
  return map[status] || status
}

// ==================== 生命周期 ====================
onMounted(() => {
  handleSync()
  startAutoRefresh()
})

onBeforeUnmount(() => {
  stopAutoRefresh()
})
</script>

<template>
  <div v-if="isMobileMode" class="mobile-device-center" v-loading.fullscreen.lock="syncLoading" element-loading-text="正在同步设备，请稍候...">
    <div class="mobile-toolbar">
      <div>
        <h2>设备状态</h2>
        <span>{{ devices.length }} 台设备</span>
      </div>
      <el-button type="primary" :icon="Refresh" circle @click="handleSync" :loading="syncLoading" />
    </div>

    <el-empty
      v-if="!loading && devices.length === 0"
      description="暂无设备"
      :image-size="100"
    />

    <div v-else class="mobile-device-list" v-loading="loading">
      <article
        v-for="device in devices"
        :key="device.serial"
        class="mobile-device-card"
      >
        <header class="mobile-device-card-header">
          <div class="mobile-device-title-wrap">
            <strong>{{ device.custom_name || device.market_name || device.model || device.serial }}</strong>
            <span>{{ device.platform === 'ios' ? 'iOS' : 'Android' }} {{ device.os_version || device.android_version || '—' }}</span>
          </div>
          <el-tag :type="statusTagType(device.status)" size="small" effect="dark" round>
            {{ statusLabel(device.status) }}
          </el-tag>
        </header>

        <div class="mobile-device-facts">
          <div>
            <span>厂商</span>
            <strong>{{ device.brand || '—' }}</strong>
          </div>
          <div>
            <span>分辨率</span>
            <strong>{{ device.resolution || '—' }}</strong>
          </div>
          <div class="span-2">
            <span>设备编号</span>
            <strong class="serial">{{ device.serial }}</strong>
          </div>
        </div>

        <div v-if="device.platform === 'ios' && device.status === 'WDA_DOWN'" class="mobile-ios-hint">
          WDA 未就绪，请先启动 WDA 后再执行。
        </div>

        <div class="mobile-device-actions">
          <el-button
            :icon="Picture"
            @click="handleScreenshot(device)"
            :disabled="device.status === 'OFFLINE'"
          >
            快照
          </el-button>
          <el-button
            v-if="device.platform === 'ios'"
            type="primary"
            plain
            :icon="Refresh"
            @click="handleCheckWda(device)"
            :loading="wdaCheckingSerial === device.serial"
            :disabled="device.status === 'OFFLINE' || device.status === 'BUSY'"
          >
            启动WDA
          </el-button>
          <el-button
            type="danger"
            plain
            :icon="CircleClose"
            @click="handleStopExecution(device)"
            :loading="stopLoadingSerial === device.serial"
            :disabled="device.status !== 'BUSY'"
          >
            停止执行
          </el-button>
        </div>
      </article>
    </div>

    <el-dialog
      v-model="screenshotVisible"
      :title="`实时屏幕快照 — ${screenshotDevice?.model || ''}`"
      width="92%"
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

  <div v-else class="device-center" v-loading.fullscreen.lock="syncLoading" element-loading-text="正在同步设备，请稍候...">

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
    <el-row :gutter="20" class="device-grid" v-else>
      <el-col
        v-for="device in devices"
        :key="device.serial"
        :xs="24"
        :sm="12"
        :md="12"
        :lg="8"
        :xl="6"
        class="device-grid-item"
      >
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
              <span class="info-value">{{ device.platform === 'ios' ? 'iOS' : 'Android' }} {{ device.os_version || device.android_version || '—' }}</span>
            </div>
            <div class="info-row">
              <span class="info-label">设备编号</span>
              <span class="info-value serial">{{ device.serial }}</span>
            </div>
            <div class="info-row">
              <span class="info-label">屏幕分辨率</span>
              <span class="info-value">{{ device.resolution || '—' }}</span>
            </div>
            <div v-if="device.platform === 'ios' && device.status === 'WDA_DOWN'" class="ios-hint down">
              <span>WDA 未就绪：请先启动 WebDriverAgent，设备才可用于执行。</span>
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
            <el-button
              v-if="device.platform !== 'ios'"
              type="warning"
              link
              :icon="SwitchButton"
              @click="handleReboot(device)"
              :disabled="device.status === 'OFFLINE'"
            >
              重启
            </el-button>
            <el-button
              v-if="device.platform === 'ios'"
              type="primary"
              link
              :icon="Refresh"
              @click="handleCheckWda(device)"
              :loading="wdaCheckingSerial === device.serial"
              :disabled="device.status === 'OFFLINE' || device.status === 'BUSY'"
            >
              启动WDA
            </el-button>
            <el-button
              type="danger"
              link
              :icon="Delete"
              @click="handleDeleteDevice(device)"
              :loading="deleteLoadingSerial === device.serial"
              :disabled="!isDeviceOffline(device)"
            >
              删除
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
.device-grid {
  min-width: 0;
}

.device-grid-item {
  min-width: 320px;
}

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

.ios-hint {
  margin-top: 8px;
  font-size: 12px;
  color: #909399;
  line-height: 1.4;
}

.ios-hint.down {
  color: #e6a23c;
  font-weight: 500;
}

/* 卡片 Footer */
.card-footer {
  display: flex;
  justify-content: space-between;
  gap: 2px;
  flex-wrap: nowrap;
  padding-top: 12px;
  border-top: 1px solid #ebeef5;
  min-width: 0;
}

.card-footer .el-button {
  flex: 1 1 0;
  justify-content: center;
  margin-left: 0;
  min-width: 0;
  padding: 0 2px;
  font-size: 12px;
  white-space: nowrap;
}

.card-footer :deep(.el-popconfirm__reference-wrapper) {
  display: flex;
  flex: 1 1 0;
  min-width: 0;
}

.card-footer :deep(.el-button > span) {
  display: inline-flex;
  align-items: center;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-footer :deep(.el-icon + span) {
  margin-left: 2px;
}

@media (max-width: 480px) {
  .device-center {
    padding: 12px;
  }

  .toolbar {
    align-items: flex-start;
    flex-direction: column;
    gap: 12px;
  }

  .device-grid-item {
    min-width: 0;
  }
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

.mobile-device-center {
  height: 100%;
  overflow-y: auto;
  padding: 12px;
  box-sizing: border-box;
  background: #f6f7f9;
}

.mobile-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.mobile-toolbar h2 {
  margin: 0 0 3px;
  font-size: 18px;
  color: #303133;
}

.mobile-toolbar span {
  font-size: 12px;
  color: #909399;
}

.mobile-device-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.mobile-device-card {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  background: #ffffff;
  padding: 14px;
}

.mobile-device-card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.mobile-device-title-wrap {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.mobile-device-title-wrap strong {
  font-size: 15px;
  color: #303133;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mobile-device-title-wrap span {
  font-size: 12px;
  color: #909399;
}

.mobile-device-facts {
  margin-top: 12px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.mobile-device-facts div {
  border-radius: 6px;
  background: #f6f7f9;
  padding: 8px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.mobile-device-facts .span-2 {
  grid-column: 1 / -1;
}

.mobile-device-facts span {
  font-size: 11px;
  color: #909399;
}

.mobile-device-facts strong {
  font-size: 13px;
  color: #303133;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mobile-device-facts .serial {
  font-family: 'SF Mono', 'Menlo', 'Monaco', monospace;
}

.mobile-ios-hint {
  margin-top: 10px;
  border-radius: 6px;
  background: #fdf6ec;
  color: #b88230;
  padding: 8px;
  font-size: 12px;
  line-height: 1.5;
}

.mobile-device-actions {
  margin-top: 12px;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.mobile-device-actions .el-button {
  margin-left: 0;
  min-width: 0;
}
</style>
