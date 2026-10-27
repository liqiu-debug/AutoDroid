<script setup>
import { ref, computed, nextTick, onMounted, onBeforeUnmount, onActivated, onDeactivated } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Picture, Unlock, SwitchButton, Monitor, Edit, Delete, CircleClose, Connection, Link, Download, CopyDocument } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'
import api from '@/api'
import { hasSessionToken } from '@/utils/sessionExpiry'
import { createPollingLoop } from '@/utils/polling'
import ScrcpyPlayer from '@/components/ScrcpyPlayer.vue'
import { isAppDevice, isUsbDevice, connectionBadge, appStatusBadge } from '@/utils/deviceAccess'
import { useClientMode } from '@/composables/useClientMode'
import { deviceStatusLabel as statusLabel, deviceStatusTagType as statusTagType } from '@/utils/statusMeta'

const { isMobileMode } = useClientMode()
const router = useRouter()

// ==================== 状态 ====================
const devices = ref([])
const bindingDevice = ref(null)
const bindingSerial = ref('')
const bindingBusy = ref(false)
const bindingOptions = computed(() => devices.value.filter(d => !isAppDevice(d) && d.platform === bindingDevice.value?.platform &&
  (!d.app_presence || d.app_presence.serial === bindingDevice.value?.app_presence?.serial)))
const openBinding = (device) => { bindingDevice.value = device; bindingSerial.value = device.app_presence.bound_device_serial || '' }
const saveBinding = async () => {
  bindingBusy.value = true
  try {
    await api.bindAppDevice(bindingDevice.value.app_presence.serial, bindingSerial.value)
    bindingDevice.value = null
    await fetchDevices()
    ElMessage.success('设备绑定已更新，两条连接的状态仍独立显示')
  } catch (e) { ElMessage.error(e.response?.data?.detail || e.message) }
  finally { bindingBusy.value = false }
}
const loading = ref(false)
const loadError = ref('')
const agentsExpanded = ref(false)
const agentDetailsTouched = ref(false)
const offlineAgentCount = computed(() => remoteAgents.value.filter(agent => agent.status !== 'ONLINE').length)
const agentDetailsVisible = computed(() => agentsExpanded.value || (!agentDetailsTouched.value && offlineAgentCount.value > 0))
const syncLoading = ref(false)
const wdaCheckingSerial = ref('')
const wirelessLoadingSerial = ref('')
const deleteLoadingSerial = ref('')
const stopLoadingSerial = ref('')

// 远程接入点
const remoteAgents = ref([])
const agentGuideVisible = ref(false)
const agentDeleteLoadingId = ref(null)
const agentScriptDownloading = ref(false)
const agentProbeLoadingId = ref(null)

// 快照弹窗：Android 默认嵌实时投屏（避免整图截图挤占远程链路带宽），
// iOS 或投屏不可用时回落到静态截图
const screenshotVisible = ref(false)
const screenshotLoading = ref(false)
const screenshotData = ref('')
const screenshotFormat = ref('png')
const screenshotDevice = ref(null)
const screenshotLiveMode = ref(false)

// 就地编辑
const editingSerial = ref(null)
const editingName = ref('')
const autoRefreshing = ref(false)
const DEVICE_LIST_REFRESH_INTERVAL_MS = 5000

// ==================== 方法 ====================

/** 获取当前设备列表 */
const fetchDevices = async ({ refreshIosWda = false, silent = false } = {}) => {
  // 会话已结束（401 处理/手动登出）：发起前静默停止，不发请求
  if (!hasSessionToken()) return
  if (autoRefreshing.value) return
  autoRefreshing.value = true
  if (!silent) {
    loading.value = true
  }
  try {
    const { data } = await api.getDeviceList({ refreshIosWda })
    devices.value = data
    loadError.value = ''
  } catch (e) {
    loadError.value = e.response?.data?.detail || e.message || '设备加载失败'
  } finally {
    autoRefreshing.value = false
    if (!silent) {
      loading.value = false
    }
  }
  fetchRemoteAgents()
}

/** 获取远程接入点列表（静默失败，不打扰设备主流程） */
const fetchRemoteAgents = async () => {
  if (!hasSessionToken()) return
  try {
    const { data } = await api.getDeviceAgents()
    remoteAgents.value = data.items || []
  } catch (e) {
    console.error(e)
  }
}

/** 链路速率可读化 */
const formatBps = (bps) => {
  const value = Number(bps) || 0
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)} Mbps`
  if (value >= 1_000) return `${Math.round(value / 1_000)} Kbps`
  return `${value} bps`
}

/** 接入点芯片上的链路摘要：RTT + B→A 实时吞吐 */
const agentLinkSummary = (linkQuality) => {
  if (!linkQuality) return ''
  const parts = []
  if (linkQuality.rtt_ms != null) parts.push(`RTT ${Math.round(linkQuality.rtt_ms)}ms`)
  parts.push(`↑${formatBps(linkQuality.up_bps)}`)
  return parts.join(' · ')
}

/** 主动探测 B→A 上行带宽（探测期间会短暂挤占投屏带宽，属预期） */
const handleProbeAgentLink = async (agent) => {
  agentProbeLoadingId.value = agent.id
  try {
    const { data } = await api.probeDeviceAgentLink(agent.id)
    ElMessage.success(
      `${agent.name} B→A 实测吞吐 ${formatBps(data.bps)}（${Math.round(data.bytes / 1024)}KB / ${data.elapsed_ms}ms）`
    )
    fetchRemoteAgents()
  } catch (e) {
    ElMessage.error('带宽探测失败：' + (e.response?.data?.detail || e.message))
  } finally {
    agentProbeLoadingId.value = null
  }
}

/** 一键同步物理设备 */
const handleSync = async () => {
  if (syncLoading.value) return
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

/** 打开快照弹窗：Android 先试实时投屏，iOS 直接静态截图 */
const handleScreenshot = async (device) => {
  screenshotDevice.value = device
  screenshotData.value = ''
  screenshotLiveMode.value = device.platform !== 'ios'
  screenshotVisible.value = true
  if (!screenshotLiveMode.value) {
    await refreshScreenshot(device.serial)
  }
}

/** 实时投屏不可用（设备未就绪等）时回落到静态截图 */
const handleScreenshotStreamError = () => {
  if (!screenshotLiveMode.value) return
  screenshotLiveMode.value = false
  if (screenshotDevice.value) {
    refreshScreenshot(screenshotDevice.value.serial)
  }
}

/** 手动在实时投屏与静态截图之间切换 */
const switchScreenshotMode = async (live) => {
  screenshotLiveMode.value = live
  if (!live && screenshotDevice.value) {
    await refreshScreenshot(screenshotDevice.value.serial)
  }
}

/** 刷新快照 */
const refreshScreenshot = async (serial) => {
  screenshotLoading.value = true
  try {
    const { data } = await api.getDeviceScreenshot(serial)
    screenshotData.value = data.base64_img
    screenshotFormat.value = data.image_format || 'png'
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

const confirmUnlock = async (device) => {
  try {
    await ElMessageBox.confirm('确定要释放该设备锁吗？仍在执行的任务可能继续占用设备。', '释放设备锁', { confirmButtonText: '释放', cancelButtonText: '取消', type: 'warning' })
    await handleUnlock(device)
  } catch { /* User dismissed the confirmation. */ }
}
const handleMaintenance = (command, device) => {
  if (command === 'binding') return openBinding(device)
  const actions = { unlock: confirmUnlock, reboot: handleReboot, wda: handleCheckWda, wirelessOn: handleEnableWireless, wirelessOff: handleDisableWireless, delete: handleDeleteDevice }
  actions[command]?.(device)
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

// ==================== 远程接入点 ====================

/** Agent 启动命令示例（自动带当前站点地址） */
const agentCommandExample = computed(() => {
  const origin = window.location.origin
  return `python device_agent.py --server ${origin} --token <你的API Token> --name 工位名称`
})

/** 下载 Agent 脚本 */
const handleDownloadAgentScript = async () => {
  agentScriptDownloading.value = true
  try {
    const { data } = await api.downloadDeviceAgentScript()
    const url = URL.createObjectURL(new Blob([data], { type: 'text/x-python' }))
    const link = document.createElement('a')
    link.href = url
    link.download = 'device_agent.py'
    link.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    ElMessage.error('下载失败：' + (e.response?.data?.detail || e.message))
  } finally {
    agentScriptDownloading.value = false
  }
}

/** 复制 Agent 启动命令 */
const copyAgentCommand = async () => {
  try {
    await navigator.clipboard.writeText(agentCommandExample.value)
    ElMessage.success('启动命令已复制')
  } catch (e) {
    ElMessage.warning('复制失败，请手动选择文本复制')
  }
}

/** 跳转 API Token 管理页 */
const gotoApiTokens = () => {
  agentGuideVisible.value = false
  router.push({ name: 'api-tokens' })
}

/** 删除离线接入点 */
const handleDeleteAgent = async (agent) => {
  try {
    await ElMessageBox.confirm(
      `确定删除接入点「${agent.name}」吗？其设备端口映射将被释放，该接入点重新上线后按新端口接入（平台侧视为新设备记录）。`,
      '删除远程接入点',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' }
    )
    agentDeleteLoadingId.value = agent.id
    await api.deleteDeviceAgent(agent.id)
    remoteAgents.value = remoteAgents.value.filter(item => item.id !== agent.id)
    ElMessage.success(`接入点 ${agent.name} 已删除`)
  } catch (e) {
    if (!['cancel', 'close'].includes(e)) {
      ElMessage.error('删除失败：' + (e.response?.data?.detail || e.message))
    }
  } finally {
    agentDeleteLoadingId.value = null
  }
}

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

/** 启用 iOS 无线模式：读取手机 IP 改走 WiFi 直连，成功后可拔线使用 */
const handleEnableWireless = async (device) => {
  if (!device || device.platform !== 'ios') return
  wirelessLoadingSerial.value = device.serial
  try {
    const { data } = await api.enableDeviceWireless(device.serial)
    device.status = data.status || device.status
    device.wireless_enabled = true
    ElMessage.success(`已启用无线模式（${data.device_ip}），现在可以拔掉数据线`)
  } catch (e) {
    ElMessage.error('启用无线失败：' + (e.response?.data?.detail || e.message))
  } finally {
    wirelessLoadingSerial.value = ''
  }
}

/** 关闭 iOS 无线模式：删除直连配置，恢复 USB 连接策略 */
const handleDisableWireless = async (device) => {
  if (!device || device.platform !== 'ios') return
  wirelessLoadingSerial.value = device.serial
  try {
    const { data } = await api.disableDeviceWireless(device.serial)
    device.status = data.status || device.status
    device.wireless_enabled = false
    ElMessage.success('已关闭无线模式，恢复 USB 连接策略')
  } catch (e) {
    ElMessage.error('关闭无线失败：' + (e.response?.data?.detail || e.message))
  } finally {
    wirelessLoadingSerial.value = ''
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

// 设备列表 5s 轮询：在途不重叠 + 页面隐藏即暂停（utils/polling.js 统一门闩）
const deviceListPollLoop = createPollingLoop({
  intervalMs: DEVICE_LIST_REFRESH_INTERVAL_MS,
  tick: () => {
    // 401/登出后 token 已清：自动静默停表，不再空转轮询
    if (!hasSessionToken()) {
      stopAutoRefresh()
      return
    }
    return fetchDevices({ refreshIosWda: true, silent: true })
  }
})

const startAutoRefresh = () => {
  // 会话已结束：不启动轮询
  if (!hasSessionToken()) return
  deviceListPollLoop.start()
}

const stopAutoRefresh = () => {
  deviceListPollLoop.stop()
}

// ==================== 生命周期 ====================
onMounted(() => {
  handleSync()
  startAutoRefresh()
})

// KeepAlive 下用 onActivated/onDeactivated 管理轮询：离开即停表（不留给隐藏页
// 空转堆积请求），回到页面即恢复；onBeforeUnmount 兜底清理。
onActivated(() => {
  startAutoRefresh()
})

onDeactivated(() => {
  stopAutoRefresh()
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
      <el-button type="primary" :icon="Refresh" circle aria-label="同步设备" @click="handleSync" :loading="syncLoading" />
    </div>

    <el-alert v-if="loadError" type="error" :title="loadError" :closable="false" show-icon class="device-load-error" />
    <el-empty
      v-if="!loading && !loadError && devices.length === 0"
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
          <div class="mobile-device-tags">
            <el-tag v-if="connectionBadge(device)" :type="connectionBadge(device).type" size="small" effect="plain">
              {{ connectionBadge(device).label }}
            </el-tag>
            <el-tag v-if="!isAppDevice(device) && !isUsbDevice(device)" :type="statusTagType(device.status)" size="small" effect="light">
              {{ statusLabel(device.status) }}
            </el-tag>
            <el-tag v-if="!isAppDevice(device) && device.app_presence" :type="appStatusBadge(device).type" size="small" effect="plain">
              {{ appStatusBadge(device).label }}
            </el-tag>
            <el-dropdown v-if="device.app_presence" trigger="click" @command="command => handleMaintenance(command, device)">
              <el-button link aria-label="App 设备操作">更多</el-button>
              <template #dropdown><el-dropdown-menu>
                <el-dropdown-item command="binding">{{ device.app_presence.bound_device_serial ? '管理同机绑定' : '绑定有线／无线设备' }}</el-dropdown-item>
              </el-dropdown-menu></template>
            </el-dropdown>
          </div>
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

        <div v-if="!isAppDevice(device) && device.platform === 'ios' && device.status === 'WDA_DOWN'" class="mobile-ios-hint">
          WDA 未就绪：请插线点「启动WDA」；无线使用请在 WDA 正常后点「启用无线」再拔线。
        </div>

        <div v-if="!isAppDevice(device)" class="mobile-device-actions">
          <el-button
            :icon="Picture"
            @click="handleScreenshot(device)"
            :disabled="!device.host_control_available"
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
            v-if="device.platform === 'ios' && !device.wireless_enabled"
            type="success"
            plain
            :icon="Connection"
            @click="handleEnableWireless(device)"
            :loading="wirelessLoadingSerial === device.serial"
            :disabled="device.status === 'OFFLINE' || device.status === 'BUSY'"
          >
            启用无线
          </el-button>
          <el-button
            v-if="device.platform === 'ios' && device.wireless_enabled"
            type="warning"
            plain
            :icon="Connection"
            @click="handleDisableWireless(device)"
            :loading="wirelessLoadingSerial === device.serial"
            :disabled="device.status === 'BUSY'"
          >
            关闭无线
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
      <div v-if="screenshotLiveMode" class="screenshot-live-container">
        <ScrcpyPlayer
          :serial="screenshotDevice?.serial || ''"
          read-only
          @error="handleScreenshotStreamError"
        />
      </div>
      <div v-else class="screenshot-container" v-loading="screenshotLoading">
        <img
          v-if="screenshotData"
          :src="`data:image/${screenshotFormat};base64,${screenshotData}`"
          class="screenshot-img"
          alt="设备截图"
        />
        <el-empty v-else description="暂无截图" :image-size="80" />
      </div>
      <template #footer>
        <template v-if="screenshotLiveMode">
          <el-button :icon="Picture" @click="switchScreenshotMode(false)">静态截图</el-button>
        </template>
        <template v-else>
          <el-button
            v-if="screenshotDevice?.platform !== 'ios'"
            :icon="Monitor"
            @click="switchScreenshotMode(true)"
          >
            实时投屏
          </el-button>
          <el-button :icon="Refresh" @click="refreshScreenshot(screenshotDevice?.serial)" :loading="screenshotLoading">
            刷新屏幕
          </el-button>
        </template>
      </template>
    </el-dialog>
  </div>

  <div v-else class="device-center" v-loading.fullscreen.lock="syncLoading" element-loading-text="正在同步设备，请稍候...">

    <!-- 顶部工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <h2 class="page-title">设备管理中心</h2>
        <el-tag type="info" size="small" >
          {{ devices.length }} 台设备
        </el-tag>
      </div>
      <div class="toolbar-actions">
        <el-button :icon="Link" @click="agentGuideVisible = true">
          接入远程设备
        </el-button>
        <el-button type="primary" :icon="Refresh" @click="handleSync" :loading="syncLoading">
          一键同步物理设备
        </el-button>
      </div>
    </div>

    <el-alert v-if="loadError" type="error" :title="loadError" show-icon :closable="false" class="device-load-error" />
    <!-- 远程接入点 -->
    <div v-if="remoteAgents.length > 0" class="agent-strip">
      <button class="agent-strip-title" :aria-expanded="agentDetailsVisible" @click="agentDetailsTouched = true; agentsExpanded = !agentsExpanded">
        <el-icon :size="16"><Link /></el-icon>
        <span>远程接入点 · {{ remoteAgents.length }}</span>
        <el-tag v-if="offlineAgentCount" type="warning" size="small">{{ offlineAgentCount }} 个离线</el-tag>
        <span class="ad-metadata">{{ agentDetailsVisible ? '收起' : '展开' }}</span>
      </button>
      <div v-if="agentDetailsVisible" class="agent-strip-list">
        <div v-for="agent in remoteAgents" :key="agent.id" class="agent-chip" :class="{ offline: agent.status !== 'ONLINE' }">
          <span class="agent-dot" :class="{ online: agent.status === 'ONLINE' }"></span>
          <span class="agent-name">{{ agent.name }}</span>
          <el-tooltip
            :content="agent.status === 'ONLINE'
              ? `在线 · ${agent.online_device_count}/${agent.device_count} 台设备在线`
              : `离线 · 最后心跳 ${agent.last_seen_at ? agent.last_seen_at.replace('T', ' ').slice(0, 19) : '—'}`"
            placement="top"
          >
            <span class="agent-meta">{{ agent.status === 'ONLINE' ? `${agent.online_device_count} 台在线` : '离线' }}</span>
          </el-tooltip>
          <el-tooltip
            v-if="agent.status === 'ONLINE' && agent.link_quality"
            placement="top"
          >
            <template #content>
              RTT：{{ agent.link_quality.rtt_ms != null ? `${Math.round(agent.link_quality.rtt_ms)}ms（均值 ${agent.link_quality.rtt_avg_ms}ms）` : '待上报（Agent 需 ≥1.2.0）' }}<br />
              实时吞吐：B→A {{ formatBps(agent.link_quality.up_bps) }} / A→B {{ formatBps(agent.link_quality.down_bps) }}<br />
              带宽实测：{{ agent.link_quality.bandwidth_probe ? `${formatBps(agent.link_quality.bandwidth_probe.bps)} @ ${agent.link_quality.bandwidth_probe.at}` : '未探测，点「测带宽」' }}
            </template>
            <span class="agent-link">{{ agentLinkSummary(agent.link_quality) }}</span>
          </el-tooltip>
          <el-button
            v-if="agent.status === 'ONLINE'"
            class="agent-probe"
            type="primary"
            link
            size="small"
            :loading="agentProbeLoadingId === agent.id"
            @click="handleProbeAgentLink(agent)"
          >
            测带宽
          </el-button>
          <el-button
            v-if="agent.status !== 'ONLINE'"
            class="agent-delete"
            type="danger"
            link
            size="small"
            :icon="Delete"
            :loading="agentDeleteLoadingId === agent.id"
            @click="handleDeleteAgent(agent)"
          />
        </div>
      </div>
    </div>

    <!-- 空状态 -->
    <el-empty
      v-if="!loading && !loadError && devices.length === 0"
      description="暂无设备，请点击「一键同步物理设备」按钮"
      :image-size="120"
    />

    <!-- 设备卡片网格 -->
    <el-row :gutter="12" class="device-grid" v-else>
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
        <el-card class="device-card" shadow="never">
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
                <button v-else type="button" class="device-name-display" aria-label="修改设备名称" @click="startEditing(device)">
                  <span class="device-title">{{ device.custom_name || device.market_name || device.model }}</span>
                  <el-icon class="edit-icon" :size="13"><Edit /></el-icon>
                </button>
                <!-- 用户修改了名称时，显示 market_name；否则显示 model（如果不同于 market_name） -->
                <span v-if="device.custom_name && device.market_name && device.market_name !== device.custom_name" class="device-model-sub">{{ device.market_name }}</span>
                <span v-else-if="!device.custom_name && device.market_name && device.market_name !== device.model" class="device-model-sub">{{ device.model }}</span>
              </div>
              <div class="card-header-tags">
                <el-tag v-if="connectionBadge(device)" :type="connectionBadge(device).type" size="small" effect="plain">
                  {{ connectionBadge(device).label }}
                </el-tag>
                <el-tag v-if="!isAppDevice(device) && !isUsbDevice(device)" :type="statusTagType(device.status)" size="small" effect="light">
                  {{ statusLabel(device.status) }}
                </el-tag>
                <el-tag v-if="!isAppDevice(device) && device.app_presence" :type="appStatusBadge(device).type" size="small" effect="plain">
                  {{ appStatusBadge(device).label }}
                </el-tag>
              </div>
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
              <el-tooltip :content="device.serial"><span class="info-value serial" tabindex="0">{{ device.serial }}</span></el-tooltip>
            </div>
            <div v-if="device.source_serial" class="info-row">
              <span class="info-label">真实序列号</span>
              <el-tooltip :content="device.source_serial"><span class="info-value serial" tabindex="0">{{ device.source_serial }}</span></el-tooltip>
            </div>
            <div class="info-row">
              <span class="info-label">屏幕分辨率</span>
              <span class="info-value">{{ device.resolution || '—' }}</span>
            </div>
            <div v-if="!isAppDevice(device) && device.platform === 'ios' && device.status === 'WDA_DOWN'" class="ios-hint down">
              <span>WDA 未就绪：请插线点「启动WDA」；无线使用请在 WDA 正常后点「启用无线」再拔线。</span>
            </div>
          </div>

          <!-- Footer -->
          <div class="card-footer">
            <el-button v-if="!isAppDevice(device)" link :icon="Picture" @click="handleScreenshot(device)" :disabled="!device.host_control_available">查看屏幕</el-button>
            <el-button v-if="!isAppDevice(device) && device.status === 'BUSY'" link type="danger" :loading="stopLoadingSerial === device.serial" @click="handleStopExecution(device)">终止执行</el-button>
            <el-button v-if="!isAppDevice(device) && device.platform === 'ios' && device.status === 'WDA_DOWN'" link type="primary" :loading="wdaCheckingSerial === device.serial" @click="handleCheckWda(device)">启动 WDA</el-button>
            <el-dropdown v-if="!isAppDevice(device) || isDeviceOffline(device) || device.app_presence" trigger="click" @command="command => handleMaintenance(command, device)">
              <el-button link aria-label="设备维护操作">更多</el-button>
              <template #dropdown><el-dropdown-menu>
                <el-dropdown-item v-if="device.app_presence" command="binding">{{ device.app_presence.bound_device_serial ? '管理同机绑定' : '绑定有线／无线设备' }}</el-dropdown-item>
                <el-dropdown-item v-if="!isAppDevice(device)" command="unlock" :disabled="device.status === 'OFFLINE'">释放设备锁</el-dropdown-item>
                <el-dropdown-item v-if="!isAppDevice(device) && device.platform !== 'ios'" command="reboot" :disabled="device.status === 'OFFLINE'">重启设备</el-dropdown-item>
                <el-dropdown-item v-if="!isAppDevice(device) && device.platform === 'ios'" command="wda" :disabled="['OFFLINE', 'BUSY'].includes(device.status) || wdaCheckingSerial === device.serial">启动 WDA</el-dropdown-item>
                <el-dropdown-item v-if="!isAppDevice(device) && device.platform === 'ios' && !device.wireless_enabled" command="wirelessOn" :disabled="['OFFLINE', 'BUSY'].includes(device.status) || wirelessLoadingSerial === device.serial">启用无线</el-dropdown-item>
                <el-dropdown-item v-if="!isAppDevice(device) && device.platform === 'ios' && device.wireless_enabled" command="wirelessOff" :disabled="device.status === 'BUSY' || wirelessLoadingSerial === device.serial">关闭无线</el-dropdown-item>
                <el-dropdown-item v-if="isDeviceOffline(device)" command="delete" divided :disabled="deleteLoadingSerial === device.serial">删除离线设备</el-dropdown-item>
              </el-dropdown-menu></template>
            </el-dropdown>
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
      <div v-if="screenshotLiveMode" class="screenshot-live-container">
        <ScrcpyPlayer
          :serial="screenshotDevice?.serial || ''"
          read-only
          @error="handleScreenshotStreamError"
        />
      </div>
      <div v-else class="screenshot-container" v-loading="screenshotLoading">
        <img
          v-if="screenshotData"
          :src="`data:image/${screenshotFormat};base64,${screenshotData}`"
          class="screenshot-img"
          alt="设备截图"
        />
        <el-empty v-else description="暂无截图" :image-size="80" />
      </div>
      <template #footer>
        <template v-if="screenshotLiveMode">
          <el-button :icon="Picture" @click="switchScreenshotMode(false)">静态截图</el-button>
        </template>
        <template v-else>
          <el-button
            v-if="screenshotDevice?.platform !== 'ios'"
            :icon="Monitor"
            @click="switchScreenshotMode(true)"
          >
            实时投屏
          </el-button>
          <el-button :icon="Refresh" @click="refreshScreenshot(screenshotDevice?.serial)" :loading="screenshotLoading">
            刷新屏幕
          </el-button>
        </template>
      </template>
    </el-dialog>

    <!-- 接入远程设备指引 -->
    <el-dialog
      v-model="agentGuideVisible"
      title="接入远程设备（设备插在自己电脑上使用平台）"
      width="640px"
      align-center
    >
      <div class="agent-guide">
        <p class="agent-guide-intro">
          在你自己的电脑（B 机）上运行「设备接入助手」，插在 B 机上的 Android 设备会通过反向隧道注册到本平台，
          全功能可用（投屏、执行、巡检、Fastbot）。数据走 USB + 网络，无需手机连 WiFi。
        </p>
        <ol class="agent-guide-steps">
          <li>
            <strong>准备环境</strong>：B 机安装
            <a href="https://www.python.org/downloads/" target="_blank" rel="noopener">Python 3.8+</a>
            与
            <a href="https://developer.android.com/tools/releases/platform-tools" target="_blank" rel="noopener">Android platform-tools（adb）</a>，
            手机开启「USB 调试」。
          </li>
          <li>
            <strong>生成 API Token</strong>：
            <el-button type="primary" link @click="gotoApiTokens">前往 API Token 管理页</el-button>
            创建一个 Token（adk_ 开头，仅创建时可见）。
          </li>
          <li>
            <strong>下载 Agent 脚本</strong>：
            <el-button type="primary" link :icon="Download" :loading="agentScriptDownloading" @click="handleDownloadAgentScript">
              下载 device_agent.py
            </el-button>
          </li>
          <li>
            <strong>在 B 机命令行运行</strong>（把 Token 与工位名换成你的）：
            <div class="agent-command">
              <code>{{ agentCommandExample }}</code>
              <el-button link type="primary" :icon="CopyDocument" @click="copyAgentCommand" />
            </div>
          </li>
          <li>
            <strong>手机授权</strong>：首次接入会弹出两次「允许 USB 调试」（B 机密钥 + 平台服务器密钥），都点允许。
            之后回到本页点「一键同步物理设备」即可看到远程设备。
          </li>
        </ol>
        <div class="agent-guide-tips">
          注意：Agent 运行期间请勿关闭其命令行窗口；执行长任务（巡检/Fastbot）时建议关闭 B 机休眠。
          手机重启或拔插后 Agent 会自动恢复隧道。
        </div>
      </div>
    </el-dialog>

  </div>
  <el-dialog :model-value="!!bindingDevice" title="绑定同一台手机" width="min(480px, 92vw)" @close="bindingDevice = null">
    <p>请确认两条记录属于同一台手机。绑定只合并展示，App 与 USB／WDA 状态独立判断。</p>
    <el-select v-model="bindingSerial" placeholder="选择同平台设备" clearable style="width: 100%">
      <el-option label="不绑定（分别显示）" value="" />
      <el-option v-for="item in bindingOptions" :key="item.serial" :value="item.serial"
        :label="`${item.custom_name || item.market_name || item.model} · ${item.serial}`" />
    </el-select>
    <template #footer>
      <el-button @click="bindingDevice = null">取消</el-button>
      <el-button type="primary" :loading="bindingBusy" @click="saveBinding">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.device-center {
  padding: 16px;
  height: 100%;
  overflow-y: auto;
  background: var(--ad-bg);
}

/* 工具栏 */
.toolbar {
  flex-wrap: wrap; gap: 12px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  padding: 0;
  background: var(--ad-surface);
  border-radius: 8px;

}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.toolbar-actions {
  flex-wrap: wrap;
  display: flex;
  align-items: center;
  gap: 8px;
}

/* 远程接入点条带 */
.agent-strip {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 12px;
  padding: 10px 20px;
  background: var(--ad-surface);
  border-radius: 8px;

  flex-wrap: wrap;
}

.agent-strip-title {
  border: 0; background: transparent; text-align: left; font-family: inherit;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--ad-text);
  flex-shrink: 0;
}

.agent-strip-list {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.agent-chip {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 6px;
  background: var(--ad-success-soft);
  border: 1px solid var(--ad-border);
  font-size: 12px;
}

.agent-chip.offline {
  background: var(--ad-sidebar);
  border-color: var(--ad-border);
}

.agent-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--ad-muted);
  flex-shrink: 0;
}

.agent-dot.online {
  background: var(--ad-success);
}

.agent-name {
  font-weight: 600;
  color: var(--ad-text);
}

.agent-meta {
  color: var(--ad-muted);
}

.agent-link {
  color: var(--ad-primary);
  font-family: 'Courier New', monospace;
  font-size: 12px;
  cursor: default;
}

.agent-probe {
  margin-left: 2px;
  padding: 0;
}

.agent-delete {
  margin-left: 2px;
  padding: 0;
}

/* 接入指引对话框 */
.agent-guide-intro {
  margin: 0 0 12px;
  font-size: 13px;
  color: var(--ad-muted);
  line-height: 1.6;
}

.agent-guide-steps {
  margin: 0;
  padding-left: 20px;
  font-size: 13px;
  color: var(--ad-text);
  line-height: 2;
}

.agent-guide-steps a {
  color: var(--ad-primary);
  text-decoration: none;
}

.agent-command {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 6px 0;
  padding: 8px 12px;
  background: var(--ad-bg);
  border-radius: 6px;
}

.agent-command code {
  flex: 1;
  font-family: 'SF Mono', 'Menlo', 'Monaco', monospace;
  font-size: 12px;
  color: var(--ad-primary);
  word-break: break-all;
  line-height: 1.5;
}

.agent-guide-tips {
  margin-top: 12px;
  padding: 8px 12px;
  border-radius: 6px;
  background: var(--ad-warning-soft);
  color: var(--ad-warning);
  font-size: 12px;
  line-height: 1.6;
}

.page-title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
  color: var(--ad-text);
}

/* 设备卡片 */
.device-grid {
  min-width: 0;
}

.device-grid-item {
  min-width: 0;
}

.device-card {
  margin-bottom: 12px;
  border-radius: 8px;

  overflow: hidden;
}

.device-card:hover { border-color: var(--ad-border); }

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.card-header-tags {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
  flex-shrink: 0;
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
  border: 0; background: transparent; text-align: left; font: inherit; max-width: 100%;
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
  background-color: var(--ad-bg);
}

.device-name-display:hover .edit-icon {
  opacity: 1;
}

.device-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--ad-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.edit-icon {
  opacity: 1;
  color: var(--ad-muted);
  flex-shrink: 0;
  transition: opacity 0.2s, color 0.2s;
}

.edit-icon:hover {
  color: var(--ad-primary);
}

.device-model-sub {
  font-size: 12px;
  color: var(--ad-muted);
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
  padding: 4px 0;
  border-bottom: 1px solid var(--ad-border);
}

.info-row:last-child {
  border-bottom: none;
}

.info-label {
  font-size: 12px;
  color: var(--ad-muted);
  flex-shrink: 0;
}

.info-value {
  font-size: 13px;
  color: var(--ad-text);
  font-weight: 500;
  text-align: right;
}

.info-value.serial {
  font-family: 'SF Mono', 'Menlo', 'Monaco', monospace;
  font-size: 12px;
  color: var(--ad-muted);
  max-width: 65%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ios-hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--ad-muted);
  line-height: 1.4;
}

.ios-hint.down {
  color: var(--ad-warning);
  font-weight: 500;
}

/* 卡片 Footer */
.card-footer {
  display: flex;
  justify-content: space-between;
  gap: 2px;
  flex-wrap: nowrap;
  padding-top: 12px;
  border-top: 1px solid var(--ad-border);
  min-width: 0;
}

.card-footer .el-button {
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
  flex-wrap: wrap; gap: 12px;
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

.screenshot-live-container {
  height: min(70vh, 640px);
  background: #1a1a2e;
  border-radius: 8px;
  overflow: hidden;
}

.screenshot-live-container :deep(.scrcpy-player) {
  height: 100%;
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
  background: var(--ad-bg);
}

.mobile-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.mobile-toolbar h2 {
  margin: 0 0 3px;
  font-size: 20px;
  color: var(--ad-text);
}

.mobile-toolbar span {
  font-size: 14px;
  color: var(--ad-muted);
}

.mobile-device-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.mobile-device-card {
  border: 1px solid var(--ad-border);
  border-radius: 8px;
  background: var(--ad-surface);
  padding: 14px;
}

.mobile-device-card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.mobile-device-tags {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
  flex-shrink: 0;
}

.mobile-device-title-wrap {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.mobile-device-title-wrap strong {
  font-size: 15px;
  color: var(--ad-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mobile-device-title-wrap span {
  font-size: 14px;
  color: var(--ad-muted);
}

.mobile-device-facts {
  margin-top: 12px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.mobile-device-facts div {
  border-radius: 6px;
  background: var(--ad-bg);
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
  font-size: 14px;
  color: var(--ad-muted);
}

.mobile-device-facts strong {
  font-size: 14px;
  color: var(--ad-text);
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
  background: var(--ad-warning-soft);
  color: var(--ad-warning);
  padding: 8px;
  font-size: 14px;
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
.device-load-error { margin-bottom: 12px; }
.card-footer .el-dropdown { margin-left: auto; }
</style>
