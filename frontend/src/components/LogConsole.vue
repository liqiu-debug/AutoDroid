<template>
  <div class="log-console" :class="{ minimized: isMinimized }">
    <div class="console-header" role="button" tabindex="0" :aria-expanded="!isMinimized" @keydown.enter="isMinimized = !isMinimized" @keydown.space.prevent="isMinimized = !isMinimized" @click="isMinimized = !isMinimized">
      <span class="title">
        <span class="icon">📋</span>
        执行日志
        <span v-if="logs.length" class="badge">{{ logs.length }}</span>
      </span>
      <span class="status" :class="runStatus">
        {{ statusText }}
      </span>
      <span class="toggle">{{ isMinimized ? '▲' : '▼' }}</span>
    </div>
    
    <div class="console-body" ref="logContainer">
      <div v-if="logs.length === 0" class="empty">
        等待执行...
      </div>
      <div
        v-for="(log, i) in logs"
        :key="i"
        class="log-item"
        :class="log.status"
      >
        <span class="log-time">{{ formatTime(log.timestamp) }}</span>
        <span class="log-icon">
          <template v-if="log.status === 'running'">⏳</template>
          <template v-else-if="log.status === 'success'">✓</template>
          <template v-else-if="log.status === 'failed'">✗</template>
          <template v-else>📌</template>
        </span>
        <span class="log-text">
          {{ log.log }}
          <span v-if="log.suggestion" class="log-suggestion">建议: {{ log.suggestion }}</span>
        </span>
      </div>
    </div>
    
    <div class="console-footer" v-if="reportId">
      <a :href="reportUrl" download target="_blank" class="report-link">
        📥 下载测试报告
      </a>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, nextTick, onUnmounted, computed } from 'vue'
import api from '@/api'

const props = defineProps({
  caseId: {
    type: Number,
    default: null
  }
})

const emit = defineEmits(['stepUpdate', 'runStart', 'runComplete', 'runError'])

const isMinimized = ref(true)
const logs = ref([])
const runStatus = ref('idle') // idle, running, success, failed
const reportId = ref(null)
const logContainer = ref(null)
let ws = null
let launchTimer = null
const clearLaunchTimer = () => {
  if (launchTimer !== null) clearTimeout(launchTimer)
  launchTimer = null
}

const statusText = computed(() => {
  switch (runStatus.value) {
    case 'submitting': return '启动中...'
    case 'unknown': return '执行状态待确认'
    case 'running': return '执行中...'
    case 'success': return '✓ 完成'
    case 'failed': return '✗ 失败'
    case 'aborted': return '已终止'
    default: return '待执行'
  }
})

const reportUrl = computed(() => api.getReportAssetUrl(reportId.value))

// 连接 WebSocket
const connect = (caseId, envId = null, deviceSerial = null) => {
  clearLaunchTimer()
  const previous = ws
  ws = null
  previous?.close()
  
  logs.value = []
  runStatus.value = 'submitting'
  reportId.value = null
  
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  let wsUrl = `${protocol}//${window.location.host}/ws/run/${caseId}`
  
  const queryParams = []
  if (envId) queryParams.push(`env_id=${encodeURIComponent(envId)}`)
  if (deviceSerial) queryParams.push(`device_serial=${encodeURIComponent(deviceSerial)}`)
  
  if (queryParams.length > 0) {
    wsUrl += `?${queryParams.join('&')}`
  }
  
  const socket = new WebSocket(wsUrl)
  ws = socket
  const isCurrent = () => ws === socket
  let terminal = false
  let started = false
  const markUnknown = () => {
    if (!isCurrent() || terminal || runStatus.value === 'unknown') return
    clearLaunchTimer()
    runStatus.value = 'unknown'
    emit('runError', { case_id: caseId, outcomeUnknown: true, message: '执行连接已断开，启动结果待确认' })
  }
  launchTimer = setTimeout(markUnknown, 60000)
  
  socket.onopen = () => {
    if (!isCurrent()) return
    logs.value.push({
      timestamp: new Date().toISOString(),
      status: 'info',
      log: '🔗 已连接，开始执行...'
    })
  }
  
  socket.onmessage = (event) => {
    if (!isCurrent()) return
    const data = JSON.parse(event.data)
    
    if (data.type === 'run_start') {
      clearLaunchTimer()
      started = true
      runStatus.value = 'running'
      logs.value.push({
        timestamp: data.timestamp,
        status: 'info',
        log: `📋 开始执行用例: ${data.case_name} (${data.total_steps} 步)`
      })
      emit('runStart', { ...data, case_id: caseId })
    }
    
    if (data.type === 'step_update') {
      logs.value.push({
        timestamp: data.timestamp,
        status: data.status,
        log: data.log,
        stepIndex: data.step_index,
        errorCode: data.error_code,
        suggestion: data.suggestion
      })

      emit('stepUpdate', data)
      scrollToBottom()
    }
    
    if (data.type === 'run_complete') {
      clearLaunchTimer()
      terminal = true
      runStatus.value = data.status === 'ABORTED' ? 'aborted' : (data.success ? 'success' : 'failed')
      reportId.value = data.report_id
      
      logs.value.push({
        timestamp: data.timestamp,
        status: data.status === 'ABORTED' ? 'warning' : (data.success ? 'success' : 'failed'),
        log: `${data.status === 'ABORTED' ? '已终止' : (data.success ? '✓' : '✗')} 执行完成: ${data.passed} 通过, ${data.failed} 失败 (${data.total_duration}s)`
      })
      
      emit('runComplete', { ...data, case_id: caseId })
    }
    
    if (data.type === 'error') {
      if (started) markUnknown()
      else {
        clearLaunchTimer()
        terminal = true
        emit('runError', { ...data, case_id: caseId })
        runStatus.value = 'failed'
      }
      logs.value.push({
        timestamp: new Date().toISOString(),
        status: 'failed',
        log: `❌ 错误: ${data.message}`
      })
    }
  }
  
  socket.onerror = () => {
    if (!isCurrent()) return
    markUnknown()
    logs.value.push({ timestamp: new Date().toISOString(), status: 'warning', log: '执行连接异常，请查看执行状态' })
  }
  socket.onclose = () => markUnknown()

}

const scrollToBottom = () => {
  nextTick(() => {
    if (logContainer.value) {
      logContainer.value.scrollTop = logContainer.value.scrollHeight
    }
  })
}

const formatTime = (isoString) => {
  if (!isoString) return ''
  const date = new Date(isoString)
  return date.toLocaleTimeString('zh-CN')
}

const clear = () => {
  clearLaunchTimer()
  const previous = ws
  ws = null
  previous?.close()
  logs.value = []
  runStatus.value = 'idle'
  reportId.value = null
}

const appendLog = (logData) => {
  logs.value.push({
    timestamp: new Date().toISOString(),
    ...logData
  })
  scrollToBottom()
}

const setReportId = (id) => {
  reportId.value = id
}

const markAborted = () => {
  clearLaunchTimer()
  const previous = ws
  ws = null
  previous?.close()
  runStatus.value = 'aborted'
  logs.value.push({
    timestamp: new Date().toISOString(),
    status: 'warning',
    log: '执行已被用户终止'
  })
  scrollToBottom()
}

// 暴露方法给父组件
defineExpose({
  connect,
  clear,
  appendLog,
  setReportId,
  markAborted
})

onUnmounted(() => {
  clearLaunchTimer()
  const previous = ws
  ws = null
  previous?.close()
})
</script>

<style scoped>
.log-console {
  background: var(--ad-surface);
  border-radius: 8px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  border: 1px solid var(--ad-border);
  height: 200px;
  transition: height 0.3s ease;
}

.log-console.minimized {
  height: 36px;
}

.console-header {
  display: flex;
  align-items: center;
  padding: 8px 12px;
  background: var(--ad-bg);
  border-bottom: 1px solid var(--ad-border);
  cursor: pointer;
  user-select: none;
}

.console-header .title {
  flex: 1;
  color: var(--ad-text);
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 8px;
}

.console-header .icon {
  font-size: 14px;
}

.console-header .badge {
  background: var(--ad-primary);
  color: var(--ad-surface);
  font-size: 12px;
  padding: 2px 6px;
  border-radius: 10px;
}

.console-header .status {
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 12px;
  margin-right: 10px;
}

.console-header .status.idle {
  background: var(--ad-bg);
  color: var(--ad-muted);
}

.console-header .status.running {
  background: var(--ad-primary);
  color: var(--ad-surface);
  font-weight: 500;
}

.console-header .status.success {
  background: var(--ad-success);
  color: var(--ad-surface);
}

.console-header .status.failed {
  background: var(--ad-danger);
  color: var(--ad-surface);
}

.console-header .status.aborted {
  background: var(--ad-muted);
  color: var(--ad-surface);
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.7; }
}

.console-header .toggle {
  color: var(--ad-muted);
  font-size: 12px;
}

.console-body {
  flex: 1;
  overflow-y: auto;
  padding: 10px;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  font-size: 12px;
}

.console-body .empty {
  color: var(--ad-muted);
  text-align: center;
  padding: 20px;
}

.log-item {
  display: flex;
  align-items: flex-start;
  padding: 4px 0;
  border-bottom: 1px solid var(--ad-border);
}

.log-item:last-child {
  border-bottom: none;
}

.log-time {
  color: var(--ad-muted);
  margin-right: 10px;
  flex-shrink: 0;
}

.log-icon {
  margin-right: 8px;
  flex-shrink: 0;
}

.log-item.running .log-icon { color: var(--ad-primary); }
.log-item.success .log-icon { color: var(--ad-success); }
.log-item.failed .log-icon { color: var(--ad-danger); }
.log-item.info .log-icon { color: var(--ad-primary); }

.log-text {
  color: var(--ad-muted);
  word-break: break-all;
}

.log-suggestion {
  display: block;
  color: var(--ad-muted);
  font-size: 12px;
  margin-top: 2px;
}

.log-item.failed .log-text {
  color: var(--ad-danger);
}

.console-footer {
  padding: 8px 15px;
  background: var(--ad-bg);
  border-top: 1px solid var(--ad-border);
}

.report-link {
  color: var(--ad-primary);
  text-decoration: none;
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 5px;
}

.report-link:hover {
  color: var(--ad-primary);
}
.log-console.minimized .console-body, .log-console.minimized .console-footer { display: none; }
.console-header { flex-shrink: 0; min-height: 34px; box-sizing: border-box; }
</style>
