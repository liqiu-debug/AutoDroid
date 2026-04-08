<template>
  <div class="log-console" :class="{ minimized: isMinimized }">
    <div class="console-header" @click="isMinimized = !isMinimized">
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
        <span class="log-text">{{ log.log }}</span>
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

const emit = defineEmits(['stepUpdate', 'runComplete'])

const isMinimized = ref(true)
const logs = ref([])
const runStatus = ref('idle') // idle, running, success, failed
const reportId = ref(null)
const logContainer = ref(null)
let ws = null

const statusText = computed(() => {
  switch (runStatus.value) {
    case 'running': return '执行中...'
    case 'success': return '✓ 完成'
    case 'failed': return '✗ 失败'
    default: return '待执行'
  }
})

const reportUrl = computed(() => api.getReportAssetUrl(reportId.value))

// 连接 WebSocket
const connect = (caseId, envId = null, deviceSerial = null) => {
  if (ws) {
    ws.close()
  }
  
  logs.value = []
  runStatus.value = 'running'
  reportId.value = null
  
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  let wsUrl = `${protocol}//${window.location.host}/ws/run/${caseId}`
  
  const queryParams = []
  if (envId) queryParams.push(`env_id=${envId}`)
  if (deviceSerial) queryParams.push(`device_serial=${deviceSerial}`)
  
  if (queryParams.length > 0) {
    wsUrl += `?${queryParams.join('&')}`
  }
  
  ws = new WebSocket(wsUrl)
  
  ws.onopen = () => {
    logs.value.push({
      timestamp: new Date().toISOString(),
      status: 'info',
      log: '🔗 已连接，开始执行...'
    })
  }
  
  ws.onmessage = (event) => {
    const data = JSON.parse(event.data)
    
    if (data.type === 'run_start') {
      logs.value.push({
        timestamp: data.timestamp,
        status: 'info',
        log: `📋 开始执行用例: ${data.case_name} (${data.total_steps} 步)`
      })
    }
    
    if (data.type === 'step_update') {
      logs.value.push({
        timestamp: data.timestamp,
        status: data.status,
        log: data.log,
        stepIndex: data.step_index
      })
      
      emit('stepUpdate', data)
      scrollToBottom()
    }
    
    if (data.type === 'run_complete') {
      runStatus.value = data.success ? 'success' : 'failed'
      reportId.value = data.report_id
      
      logs.value.push({
        timestamp: data.timestamp,
        status: data.success ? 'success' : 'failed',
        log: `${data.success ? '✓' : '✗'} 执行完成: ${data.passed} 通过, ${data.failed} 失败 (${data.total_duration}s)`
      })
      
      emit('runComplete', data)
    }
    
    if (data.type === 'error') {
      runStatus.value = 'failed'
      logs.value.push({
        timestamp: new Date().toISOString(),
        status: 'failed',
        log: `❌ 错误: ${data.message}`
      })
    }
  }
  
  ws.onerror = (err) => {
    runStatus.value = 'failed'
    logs.value.push({
      timestamp: new Date().toISOString(),
      status: 'failed',
      log: '❌ WebSocket 连接错误'
    })
  }
  
  ws.onclose = () => {
    if (runStatus.value === 'running') {
      runStatus.value = 'idle'
    }
  }
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

// 暴露方法给父组件
defineExpose({
  connect,
  clear,
  appendLog,
  setReportId
})

onUnmounted(() => {
  if (ws) {
    ws.close()
  }
})
</script>

<style scoped>
.log-console {
  background: #fff;
  border-radius: 8px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  height: 200px;
  transition: height 0.3s ease;
}

.log-console.minimized {
  height: 40px;
}

.console-header {
  display: flex;
  align-items: center;
  padding: 10px 15px;
  background: #e6e8eb;
  border-bottom: 1px solid #ebeef5;
  cursor: pointer;
  user-select: none;
}

.console-header .title {
  flex: 1;
  color: #303133;
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 8px;
}

.console-header .icon {
  font-size: 14px;
}

.console-header .badge {
  background: #667eea;
  color: #fff;
  font-size: 11px;
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
  background: #4a5568;
  color: #a0aec0;
}

.console-header .status.running {
  background: #3182ce;
  color: #fff;
  animation: pulse 1.5s infinite;
}

.console-header .status.success {
  background: #38a169;
  color: #fff;
}

.console-header .status.failed {
  background: #e53e3e;
  color: #fff;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.7; }
}

.console-header .toggle {
  color: #8892b0;
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
  color: #909399;
  text-align: center;
  padding: 20px;
}

.log-item {
  display: flex;
  align-items: flex-start;
  padding: 4px 0;
  border-bottom: 1px solid #ebeef5;
}

.log-item:last-child {
  border-bottom: none;
}

.log-time {
  color: #718096;
  margin-right: 10px;
  flex-shrink: 0;
}

.log-icon {
  margin-right: 8px;
  flex-shrink: 0;
}

.log-item.running .log-icon { color: #3182ce; }
.log-item.success .log-icon { color: #38a169; }
.log-item.failed .log-icon { color: #e53e3e; }
.log-item.info .log-icon { color: #667eea; }

.log-text {
  color: #606266;
  word-break: break-all;
}

.log-item.failed .log-text {
  color: #fc8181;
}

.console-footer {
  padding: 8px 15px;
  background: #fafafa;
  border-top: 1px solid #ebeef5;
}

.report-link {
  color: #667eea;
  text-decoration: none;
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 5px;
}

.report-link:hover {
  color: #764ba2;
}
</style>
