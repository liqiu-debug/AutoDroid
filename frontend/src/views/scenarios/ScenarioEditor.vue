<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { VueDraggable } from 'vue-draggable-plus'
import api from '@/api'
import { ElMessage } from 'element-plus'
import { 
  VideoPlay, Delete, Rank, Document, 
  Search, Upload, ArrowLeft, FolderOpened
} from '@element-plus/icons-vue'
import LogConsole from '@/components/LogConsole.vue'
import { createUuid } from '@/utils/uuid'

const route = useRoute()
const router = useRouter()

// Data
const scenarioId = computed(() => route.params.id)
const currentScenario = ref({ name: '' })
const caseLibrary = ref([])
const scenarioSteps = ref([])
const caseSearchQuery = ref('')
const caseTreeData = ref([])
const caseTreeRef = ref(null)

const loading = ref(false)
const running = ref(false)
const logConsoleRef = ref(null)

const envId = ref(null)
const environments = ref([])

const caseTreeProps = {
    children: 'children',
    label: 'name',
    isLeaf: (data) => data.type === 'case'
}

const filterCaseNode = (value, data) => {
    if (!value) return true
    if (data.type === 'case') {
        return data.name.toLowerCase().includes(value.toLowerCase())
    }
    return true
}

watch(caseSearchQuery, (val) => {
    caseTreeRef.value?.filter(val)
})

// Init
const devices = ref([])
const devicesLoading = ref(false)

const fetchDevices = async () => {
  devicesLoading.value = true
  try {
    const res = await api.getDeviceList()
    devices.value = res.data
  } catch (err) {
    ElMessage.error('获取设备列表失败')
  } finally {
    devicesLoading.value = false
  }
}

onMounted(async () => {
    if (scenarioId.value) {
        // Must fetch cases first to ensure dictionary is ready
        await fetchCases()
        await Promise.all([
            fetchScenarioDetails(),
            fetchScenarioSteps()
        ])
    } else {
        // New Scenario
        currentScenario.value = { name: `新建场景 ${new Date().toLocaleDateString()}` }
        fetchCases()
    }
    fetchEnvironments()
})

const fetchEnvironments = async () => {
    try {
        const { data } = await api.getEnvironments()
        environments.value = data
        if (data && data.length > 0 && !envId.value) {
            envId.value = data[0].id
        }
    } catch (error) {
        console.error('获取环境列表失败', error)
    }
}

// Actions
const fetchCases = async () => {
  try {
    const [casesRes, treeRes] = await Promise.all([
      api.getTestCases({ skip: 0, limit: 1000 }),
      api.getFolderTree()
    ])
    const cases = casesRes.data.items || casesRes.data
    caseLibrary.value = cases.map(c => ({
      ...c,
      uuid: createUuid(),
      type: 'case'
    }))

    const { tree, all_cases } = treeRes.data
    const allNode = { id: 'all', name: '所有用例', type: 'all', children: all_cases || [] }
    caseTreeData.value = [allNode, ...tree]
  } catch (err) {
    ElMessage.error('加载用例库失败')
  }
}

const fetchScenarioDetails = async () => {
    if (!scenarioId.value) return
    try {
        const res = await api.getScenario(scenarioId.value)
        currentScenario.value = res.data
    } catch (err) {
        ElMessage.error('加载场景详情失败')
    }
}

const fetchScenarioSteps = async () => {
  if (!scenarioId.value) return
  loading.value = true
  try {
    const res = await api.getScenarioSteps(scenarioId.value)
    scenarioSteps.value = res.data.map(step => {
        const originalCase = caseLibrary.value.find(c => c.id === step.case_id)
        return {
            ...(originalCase || { name: '未知用例', id: step.case_id }), 
            uuid: createUuid(),
            alias: step.alias,
            scenario_step_id: step.id,
            id: step.case_id 
        }
    })
  } catch (err) {
    ElMessage.error('加载场景步骤失败')
  } finally {
    loading.value = false
  }
}

const saveSteps = async () => {
  if (!currentScenario.value.name.trim()) {
      ElMessage.warning('请输入场景名称')
      return
  }

  loading.value = true
  try {
    let targetId = scenarioId.value
    
    // Create if new
    if (!targetId) {
         const res = await api.createScenario({ name: currentScenario.value.name })
         targetId = res.data.id
         // Update URL without reloading to keep state
         await router.replace(`/ui/scenarios/${targetId}/edit`)
         ElMessage.success('场景创建成功')
    } else {
         // Update Name
         await api.updateScenario(targetId, { name: currentScenario.value.name })
    }

    const payload = scenarioSteps.value.map((step, index) => ({
      case_id: step.id,
      order: index + 1,
      alias: step.alias || step.name
    }))
    
    await api.updateScenarioSteps(targetId, payload)
    ElMessage.success('保存成功')
  } catch (err) {
    ElMessage.error('保存失败: ' + err.message)
  } finally {
    loading.value = false
  }
}

const summarizeScenarioPrecheckFailure = (payload) => {
  if (!payload || typeof payload !== 'object') return '预检失败'

  const failCase = (payload.cases || []).find(item => item?.status === 'FAIL')
  if (failCase) {
    const caseName = failCase.alias || failCase.case_name || `Case#${failCase.case_id || '?'}`
    const reason = failCase.reason || '用例预检失败'
    return `${caseName}: ${reason}`
  }

  if (!payload.has_runnable_cases) return '全部用例将被跳过（当前设备无可执行步骤）'
  return '预检失败'
}

const summarizeHttpDetail = (err) => {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string' && detail) return detail
  if (detail && typeof detail === 'object') {
    if (detail.message) return detail.message
    if (Array.isArray(detail.items) && detail.items.length > 0) {
      const first = detail.items[0]
      if (first?.device_serial || first?.reason) {
        return `${first.device_serial || '未知设备'} - ${first.reason || '预检失败'}`
      }
    }
  }
  return err?.message || '请求失败'
}

const precheckScenarioOnDevice = async (serial) => {
  try {
    const { data } = await api.precheckScenario(scenarioId.value, envId.value, serial)
    if (data?.ok) return { ok: true }
    return { ok: false, reason: summarizeScenarioPrecheckFailure(data) }
  } catch (err) {
    return { ok: false, reason: `预检接口调用失败: ${summarizeHttpDetail(err)}` }
  }
}

const runScenario = async (selectedSerial) => {
  await saveSteps()
  
  // If save failed or still no ID, stop
  if (!scenarioId.value) return false

  if (selectedSerial) {
    const check = await precheckScenarioOnDevice(selectedSerial)
    if (!check.ok) {
      ElMessage.error(`运行前预检失败: ${check.reason}`)
      return false
    }
  }

  running.value = true
  if (logConsoleRef.value) {
      logConsoleRef.value.clear()
  }

  // WebSocket Connection
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  
  let wsUrl = `${protocol}//${window.location.host}/api/scenarios/ws/run/${scenarioId.value}`
  const queryParams = []
  if (envId.value) queryParams.push(`env_id=${envId.value}`)
  if (selectedSerial) queryParams.push(`device_serial=${selectedSerial}`)
  
  if (queryParams.length > 0) {
      wsUrl += `?${queryParams.join('&')}`
  }
  
  const ws = new WebSocket(wsUrl)
  
  ws.onopen = () => {
      if (logConsoleRef.value) {
          logConsoleRef.value.appendLog({ status: 'info', log: '🔌 WebSocket 连接已建立' })
      }
  }

  ws.onmessage = (event) => {
      try {
          const data = JSON.parse(event.data)
          if (data.type === 'log') {
               const statusMap = {
                   'info': 'info',
                   'success': 'success',
                   'warning': 'warning',
                   'error': 'failed'
               }
               if (logConsoleRef.value) {
                   logConsoleRef.value.appendLog({
                       status: statusMap[data.level] || 'info', 
                       log: data.message,
                       timestamp: data.timestamp
                   })
               }
          } else if (data.type === 'run_complete') {
              running.value = false
              if (data.success) {
                  ElMessage.success('场景执行完成')
              } else {
                  ElMessage.warning('场景执行完成，存在失败步骤')
              }
              
              if (data.report_id && logConsoleRef.value) {
                  logConsoleRef.value.setReportId(data.report_id)
              }
              ws.close()
          }
      } catch (e) {
          console.error('Parse msg error', e)
      }
  }

  ws.onerror = (e) => {
      console.error('WebSocket error', e)
      if (logConsoleRef.value) {
          logConsoleRef.value.appendLog({ status: 'failed', log: '❌ WebSocket 连接错误' })
      }
      running.value = false
      ElMessage.error('连接服务失败')
  }

  ws.onclose = () => {
      if (running.value) {
           running.value = false
           if (logConsoleRef.value) {
               logConsoleRef.value.appendLog({ status: 'warning', log: '🔌 连接已断开' })
           }
      }
  }
  return true
}

const selectedStep = ref(null)

const runDialogVisible = ref(false)
const multiRunForm = ref({
  deviceSerials: []
})

const submitMultiRun = async () => {
  if (multiRunForm.value.deviceSerials.length === 0) {
    ElMessage.warning('请至少选择一台设备')
    return
  }
  if (!scenarioId.value) {
    ElMessage.warning('请先保存场景')
    return
  }

  // Single Selection: Use Real-time WebSocket execution
  if (multiRunForm.value.deviceSerials.length === 1) {
      const started = await runScenario(multiRunForm.value.deviceSerials[0])
      if (started) runDialogVisible.value = false
      return
  }

  // Multiple Selection: Background Batch Execution
  try {
    const runnable = []
    const blocked = []
    for (const serial of multiRunForm.value.deviceSerials) {
      const check = await precheckScenarioOnDevice(serial)
      if (check.ok) runnable.push(serial)
      else blocked.push({ device_serial: serial, reason: check.reason })
    }

    if (runnable.length === 0) {
      const first = blocked[0]
      ElMessage.error(`运行前预检未通过：${first ? `${first.device_serial} - ${first.reason}` : '无可执行设备'}`)
      return
    }

    const { data } = await api.runScenario(scenarioId.value, envId.value, runnable)
    const backendBlocked = Array.isArray(data?.blocked_prechecks) ? data.blocked_prechecks : []
    const allBlocked = blocked.concat(backendBlocked)
    if (allBlocked.length > 0) {
      const first = allBlocked[0]
      ElMessage.warning(`已在 ${runnable.length} 台设备启动；${allBlocked.length} 台预检失败（示例：${first.device_serial} - ${first.reason}）`)
    } else {
      ElMessage.success(`后台已开始在 ${runnable.length} 台设备上执行场景`)
    }
    runDialogVisible.value = false
  } catch (err) {
    ElMessage.error('启动批量执行失败: ' + summarizeHttpDetail(err))
  }
}

const openRunDialog = async () => {
  if (!scenarioId.value) {
    ElMessage.warning('请先保存场景')
    return
  }
  if (scenarioSteps.value.length === 0) {
    ElMessage.warning('场景没有步骤')
    return
  }
  multiRunForm.value.deviceSerials = []
  runDialogVisible.value = true
  await fetchDevices()
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

const isDeviceSelectable = (device) => device?.status === 'IDLE'

const deviceUnavailableReason = (device) => {
  if (!device) return ''
  if (device.status === 'WDA_DOWN') return 'WDA 未就绪'
  if (device.status === 'BUSY') return '设备正忙'
  return ''
}

const hasWdaDownDevice = computed(() => devices.value.some(d => d.status === 'WDA_DOWN'))

const handleStepClick = (step) => {
    selectedStep.value = step
}

const handleCaseTreeClick = (data) => {
    if (data.type !== 'case') return
    const libCase = caseLibrary.value.find(c => c.id === data.case_id)
    if (!libCase) return
    addCaseToScenario(libCase)
}

const addCaseToScenario = (item) => {
    const newStep = {
        ...item,
        uuid: createUuid(),
        alias: item.name 
    }
    scenarioSteps.value.push(newStep)
    ElMessage.success(`已添加用例: ${item.name}`)
}

const goBack = () => {
    router.push('/ui/scenarios')
}

// Preview Helpers
const expandedPreviewSteps = ref(new Set())

const toggleStepExpand = (index) => {
    if (expandedPreviewSteps.value.has(index)) {
        expandedPreviewSteps.value.delete(index)
    } else {
        expandedPreviewSteps.value.add(index)
    }
}

const isStepExpanded = (index) => expandedPreviewSteps.value.has(index)

const actionOptions = [
  { value: 'click', label: '点击 (Click)' },
  { value: 'click_image', label: '图像点击 (Click Image)' },
  { value: 'input', label: '输入 (Input)' },
  { value: 'wait_until_exists', label: '等待元素 (Wait)' },
  { value: 'assert_text', label: '文本断言 (Assert)' },
  { value: 'assert_image', label: '图像断言 (Assert Image)' },
  { value: 'swipe', label: '滑动 (Swipe)' },
  { value: 'sleep', label: '强制等待 (Sleep)' },
  { value: 'extract_by_ocr', label: 'OCR提取变量 (OCR)' },
  { value: 'start_app', label: '启动应用 (Start App)' },
  { value: 'stop_app', label: '停止应用 (Stop App)' }
]

const getStepTitle = (step) => {
  const actionLabel = actionOptions.find(a => a.value === step.action)?.label || step.action
  let target = step.selector ? (step.selector.length > 25 ? step.selector.slice(0, 25) + '...' : step.selector) : '?'
  if (step.action === 'sleep') {
    target = step.value ? `${step.value}s` : '5s'
  } else if (step.action === 'assert_text') {
    target = step.value || '-'
  } else if (step.action === 'assert_image') {
    const matchMode = step.options?.match_mode === 'not_exists' ? '不存在' : '存在'
    target = `${matchMode} ${target}`
  } else if (step.action === 'extract_by_ocr') {
    target = step.value || 'OCR_VAR'
  }
  return `${actionLabel.split(' ')[0]} → ${target}`
}

const getActionColor = (action) => {
  const colors = {
    click: '#667eea',
    click_image: '#764ba2',
    input: '#f093fb',
    wait_until_exists: '#4facfe',
    assert_text: '#fa709a',
    assert_image: '#ff8a65',
    swipe: '#30cfd0',
    sleep: '#e6a23c',
    extract_by_ocr: '#67c23a',
    start_app: '#67c23a',
    stop_app: '#f56c6c'
  }
  return colors[action] || '#909399'
}
</script>

<template>
  <el-container class="scenario-layout">
    <!-- Global Header is now in App.vue -->
    <el-container class="scenario-builder">
    
    <!-- Left: Case Library -->
    <el-aside width="300px" class="pane left-pane">
        <!-- New Header Area in Left Pane -->
        <div class="left-header">
             <el-button link :icon="ArrowLeft" @click="goBack" class="back-btn" />
             <el-input 
                 v-model="currentScenario.name" 
                 class="scenario-name-input" 
                 placeholder="请输入场景名称" 
                 clearable
             />
        </div>
        
        <div class="pane-header">
            <span class="title">用例库 (点击添加)</span>
        </div>
        <div class="search-box">
             <el-input v-model="caseSearchQuery" placeholder="搜索用例..." :prefix-icon="Search" size="small" clearable />
        </div>
        <div class="list-content">
            <el-tree
                ref="caseTreeRef"
                :data="caseTreeData"
                :props="caseTreeProps"
                node-key="id"
                :default-expanded-keys="[]"
                :expand-on-click-node="true"
                :filter-node-method="filterCaseNode"
                @node-click="handleCaseTreeClick"
            >
                <template #default="{ data }">
                    <div class="case-tree-node" :class="{ 'is-case': data.type === 'case' }">
                        <el-icon v-if="data.type !== 'case'" class="node-folder-icon"><FolderOpened /></el-icon>
                        <el-icon v-else class="node-case-icon"><Document /></el-icon>
                        <span class="node-label">{{ data.name }}</span>
                        <el-tag v-if="data.type === 'case'" size="small" type="info" class="node-id-tag">ID: {{ data.case_id }}</el-tag>
                    </div>
                </template>
            </el-tree>
        </div>
    </el-aside>

    <!-- Center: Orchestration + Logs -->
    <el-main class="center-container">
        <!-- Orchestration (Top) -->
        <div class="pane orchestration-pane">
            <div class="pane-header">
                <div class="header-left">
                        <span class="title">场景编排</span>
                        <el-tag v-if="scenarioSteps.length > 0" effect="plain" class="scenario-tag">{{ scenarioSteps.length }} 步骤</el-tag>
                </div>
                <div class="actions" style="display: flex; align-items: center;">
                    <el-select
                      v-model="envId"
                      placeholder="运行环境"
                      style="width: 110px; margin-right: 10px;"
                    >
                      <el-option
                        v-for="env in environments"
                        :key="env.id"
                        :label="env.name"
                        :value="env.id"
                      />
                    </el-select>
                    <el-button 
                      type="success" 
                      :icon="VideoPlay"
                      @click="openRunDialog" 
                      :disabled="running"
                      style="margin-right: 12px"
                    >
                      运行
                    </el-button>
                    <el-button type="primary" :icon="Upload" @click="saveSteps" :loading="loading">保存</el-button>
                </div>
            </div>
            
            <div class="step-list-header">
                <div class="col-idx">#</div>
                <div class="col-name">用例名称</div>
                <div class="col-alias">步骤别名 (Alias)</div>
                <div class="col-action">操作</div>
            </div>

            <div class="list-content step-content">
                    <VueDraggable
                    v-model="scenarioSteps"
                    group="scenario"
                    handle=".drag-handle"
                    class="step-draggable"
                    animation="200"
                    >
                        <div 
                        v-for="(step, index) in scenarioSteps" 
                        :key="step.uuid" 
                        class="step-item"
                        :class="{ active: selectedStep && selectedStep.uuid === step.uuid }"
                        @click="handleStepClick(step)"
                        >
                        <div class="col-idx drag-handle">
                            <el-icon><Rank /></el-icon>
                            {{ index + 1 }}
                        </div>
                        <div class="col-name">
                                <el-icon><Document /></el-icon>
                                {{ step.name }}
                        </div>
                        <div class="col-alias">
                            <el-input v-model="step.alias" size="small" placeholder="可选别名" @click.stop />
                        </div>
                        <div class="col-action">
                                <el-button 
                                :icon="Delete" 
                                size="small" 
                                circle 
                                type="danger" 
                                @click.stop="scenarioSteps.splice(index, 1); if(selectedStep?.uuid === step.uuid) selectedStep = null"
                            />
                        </div>
                        </div>
                    </VueDraggable>
                    <el-empty v-if="scenarioSteps.length === 0" description="拖拽左侧的用例至此" :image-size="80" />
            </div>
        </div>

        <!-- Logs (Bottom) -->
        <LogConsole ref="logConsoleRef" />
    </el-main>

    <!-- Right: Case Preview -->
    <el-aside width="350px" class="pane right-pane">
         <div class="pane-header">
            <span class="title">用例预览</span>
         </div>
         <div class="preview-content" v-if="selectedStep">
             <div class="preview-info">
                 <div class="info-row">
                     <span class="label">用例名称:</span>
                     <span class="value">{{ selectedStep.name }}</span>
                 </div>
                 <div class="info-row">
                     <span class="label">别名:</span>
                     <span class="value">{{ selectedStep.alias || '-' }}</span>
                 </div>
                 <div class="info-row">
                     <span class="label">步骤数:</span>
                     <span class="value">{{ selectedStep.steps ? selectedStep.steps.length : 0 }}</span>
                 </div>
             </div>
             <el-divider content-position="left">步骤详情</el-divider>
             <div class="preview-steps">
                 <div 
                    v-for="(step, index) in selectedStep.steps" 
                    :key="index" 
                    class="preview-step-card"
                    :style="{ '--action-color': getActionColor(step.action) }"
                 >
                     <div class="preview-step-header" @click="toggleStepExpand(index)">
                         <div class="preview-step-index">{{ index + 1 }}</div>
                         <div class="preview-step-title">{{ getStepTitle(step) }}</div>
                         <el-icon class="expand-icon" :class="{ expanded: isStepExpanded(index) }">
                            <ArrowLeft />
                         </el-icon>
                     </div>
                     
                     <transition name="expand">
                        <div v-if="isStepExpanded(index)" class="preview-step-body">
                            <div class="form-row">
                                <label>动作:</label>
                                <span>{{ step.action }}</span>
                            </div>
                            <div class="form-row">
                                <label>定位方式:</label>
                                <span>{{ step.selector_type || '-' }}</span>
                            </div>
                            <div class="form-row" v-if="!['swipe', 'sleep', 'extract_by_ocr'].includes(step.action)">
                                <label>选择器:</label>
                                <span class="break-text">{{ step.selector || '-' }}</span>
                            </div>
                            <div class="form-row" v-if="step.action === 'swipe'">
                                <label>方向:</label>
                                <span>{{ step.selector === 'up' ? '上滑' : step.selector === 'down' ? '下滑' : step.selector === 'left' ? '左滑' : '右滑' }}</span>
                            </div>
                            <div class="form-row" v-if="step.action === 'sleep'">
                                <label>等待时间:</label>
                                <span>{{ step.value || '5' }} 秒</span>
                            </div>
                            <template v-if="step.action === 'extract_by_ocr'">
                                <div class="form-row">
                                    <label>截取区域:</label>
                                    <span class="break-text">{{ step.selector || '-' }}</span>
                                </div>
                                <div class="form-row">
                                    <label>存入变量:</label>
                                    <span class="break-text">{{ step.value || '-' }}</span>
                                </div>
                                <div class="form-row">
                                    <label>提取规则:</label>
                                    <span>{{ step.options?.extract_rule || 'preset' }}</span>
                                </div>
                                <div class="form-row" v-if="step.options?.extract_rule === 'preset'">
                                    <label>模板类型:</label>
                                    <span>{{ step.options?.preset_type || '-' }}</span>
                                </div>
                                <div class="form-row" v-if="step.options?.extract_rule === 'boundary'">
                                    <label>边界规则:</label>
                                    <span class="break-text">左: {{ step.options?.left_bound || '-' }} / 右: {{ step.options?.right_bound || '-' }}</span>
                                </div>
                                <div class="form-row" v-if="step.options?.extract_rule === 'regex'">
                                    <label>正则表达式:</label>
                                    <span class="break-text">{{ step.options?.custom_regex || '-' }}</span>
                                </div>
                            </template>
                            <div class="form-row" v-if="['input', 'assert_text'].includes(step.action)">
                                <label>值:</label>
                                <span class="break-text">{{ step.value || '-' }}</span>
                            </div>
                            <div class="form-row">
                                <label>描述:</label>
                                <span class="break-text">{{ step.description || '-' }}</span>
                            </div>
                        </div>
                     </transition>
                 </div>
                 <el-empty v-if="!selectedStep.steps || selectedStep.steps.length === 0" description="暂无步骤" :image-size="40" />
             </div>
         </div>
         <el-empty v-else description="点击左侧编排列表查看详情" :image-size="100" />
    </el-aside>

    <!-- 多设备运行弹窗 -->
    <el-dialog
      v-model="runDialogVisible"
      title="选择多设备执行"
      width="400px"
    >
      <el-form v-loading="devicesLoading" :model="multiRunForm" label-width="100px">
        <el-form-item label="设备列表">
          <el-select
            v-model="multiRunForm.deviceSerials"
            placeholder="请选择执行设备"
            multiple
            collapse-tags
            collapse-tags-tooltip
            :disabled="devicesLoading"
            style="width: 100%"
          >
            <el-option
              v-for="d in devices"
              :key="d.serial"
              :label="d.custom_name || d.market_name || d.model || d.serial"
              :value="d.serial"
              :disabled="!isDeviceSelectable(d)"
            >
              <div style="display: flex; justify-content: space-between; align-items: center; width: 100%;">
                <span>{{ d.custom_name || d.market_name || d.model || d.serial }}</span>
                <div style="display: flex; align-items: center; gap: 6px;">
                  <el-tag :type="statusTagType(d.status)" size="small">{{ statusLabel(d.status) }}</el-tag>
                  <span v-if="deviceUnavailableReason(d)" style="font-size: 12px; color: #e6a23c;">
                    {{ deviceUnavailableReason(d) }}
                  </span>
                </div>
              </div>
            </el-option>
          </el-select>
          <div v-if="hasWdaDownDevice" class="run-warning-hint">
            检测到 iOS 设备 WDA 异常，需在设备中心先执行“检测WDA”。
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="runDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="devicesLoading" :disabled="devicesLoading" @click="submitMultiRun">确定执行</el-button>
        </span>
      </template>
    </el-dialog>
  </el-container>
  </el-container>
</template>

<style scoped>
.scenario-layout {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #f2f3f5;
  overflow: hidden;
}

.run-warning-hint {
  margin-top: 6px;
  font-size: 12px;
  color: #e6a23c;
}

/* app-header styles removed */

.left-header {
    background: #fff;
    height: 50px;
    display: flex;
    align-items: center;
    padding: 0 20px;
    border-bottom: 1px solid #e4e7ed;
    gap: 10px;
}

.back-btn {
    font-size: 20px;
    color: #606266;
    padding: 0;
}
.back-btn:hover {
    color: #409eff;
}

.scenario-name-input {
    flex: 1;
    font-size: 14px;
    font-weight: bold;
}

.scenario-name-input :deep(.el-input__wrapper) {
    background-color: transparent !important;
    box-shadow: none !important;
    padding-left: 0;
}

.scenario-name-input :deep(.el-input__inner) {
    color: #303133;
    font-weight: bold;
}

.scenario-name-input :deep(.el-input__wrapper:hover),
.scenario-name-input :deep(.el-input__wrapper.is-focus) {
    box-shadow: none !important;
    background-color: rgba(0, 0, 0, 0.05) !important;
}

.scenario-builder {
  flex: 1;
  overflow: hidden;
  display: flex;
  gap: 10px;
  padding: 10px;
}

.pane {
    background: #fff;
    display: flex;
    flex-direction: column;
    height: 100%;
    border-radius: 4px;
    box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.1);
    overflow: hidden; 
}

.center-container {
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
    overflow: hidden;
}

.orchestration-pane {
    flex: 1; /* Auto expand */
    min-height: 0;
}

.logs-pane {
    height: 250px;
    flex-shrink: 0;
}

.pane-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 20px;
  border-bottom: 1px solid #e4e7ed;
  height: 50px;
  box-sizing: border-box;
  background-color: #fafafa;
  flex-shrink: 0;
}

.title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

.search-box {
  padding: 10px 12px;
  border-bottom: 1px solid #f0f2f5;
}

.list-content {
    flex: 1;
    overflow-y: auto;
    padding: 8px;
}

.case-tree-node {
    display: flex;
    align-items: center;
    font-size: 13px;
    width: 100%;
    overflow: hidden;
}

.case-tree-node.is-case {
    cursor: pointer;
}

.case-tree-node.is-case:hover .node-label {
    color: #409eff;
}

.node-folder-icon {
    color: #e6a23c;
    margin-right: 5px;
    flex-shrink: 0;
}

.node-case-icon {
    color: #909399;
    margin-right: 5px;
    flex-shrink: 0;
}

.node-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
}

.node-id-tag {
    flex-shrink: 0;
    margin-left: 6px;
}

:deep(.left-pane .el-tree-node__content) {
    height: 32px;
}

.header-left {
    display: flex;
    align-items: center;
    gap: 10px;
}

.step-list-header {
    display: flex;
    padding: 8px 16px;
    background: #f5f7fa;
    color: #909399;
    font-size: 12px;
    font-weight: bold;
    border-bottom: 1px solid #e4e7ed;
    margin-bottom: 10px;
}

.step-content {
    padding: 0 10px; /* Add horizontal padding to container */
}

.step-item {
    display: flex;
    align-items: center;
    padding: 8px 16px;
    margin-bottom: 8px; /* Distinct separation */
    border: 1px solid #e4e7ed; /* Full border */
    border-radius: 4px;
    background: #fff;
    transition: all 0.2s;
    cursor: pointer;
}
.step-item:hover { 
    background: #fafafa;
    border-color: #c0c4cc;
    box-shadow: 0 2px 8px rgba(0,0,0,0.05);
}
.step-item.active {
    border-color: #409eff;
    background-color: #ecf5ff;
    box-shadow: 0 2px 8px rgba(64, 158, 255, 0.15);
}

.col-idx { width: 40px; display: flex; align-items: center; gap: 5px; color: #c0c4cc; cursor: grab; }
.col-name { flex: 2; display: flex; align-items: center; gap: 8px; font-weight: 500; color: #303133; }
.col-alias { flex: 1; padding-right: 10px; }
.col-action { width: 40px; text-align: center; }

.step-draggable { min-height: 100px; }




/* Preview Pane Styles */
.preview-content {
    padding: 15px;
    overflow-y: auto;
    flex: 1;
}
.preview-info {
    font-size: 13px;
    margin-bottom: 10px;
}
.info-row {
    display: flex;
    margin-bottom: 6px;
}
.info-row .label {
    width: 70px;
    color: #909399;
}
.info-row .value {
    flex: 1;
    color: #303133;
    font-weight: 500;
}
.preview-steps {
    display: flex;
    flex-direction: column;
    gap: 8px;
}
.preview-step-item {
    display: flex;
    gap: 10px;
    padding: 8px;
    background: #f8f9fa;
    border-radius: 4px;
    border: 1px solid #ebeef5;
    font-size: 12px;
}
.step-num {
    width: 20px;
    color: #909399;
    font-weight: bold;
    display: flex;
    justify-content: center;
    align-items: flex-start;
    padding-top: 2px;
}
.step-detail {
    flex: 1;
    overflow: hidden;
}
.step-action {
    font-weight: bold;
    color: #409eff;
    margin-bottom: 2px;
}
.step-desc {
    color: #606266;
    margin-bottom: 2px;
}
.step-target {
    color: #909399;
    font-family: monospace;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

/* Step Builder Style Replication for Preview */
.preview-step-card {
    background: #fafafa;
    border-radius: 8px;
    border-left: 3px solid var(--action-color);
    overflow: hidden;
    margin-bottom: 8px;
    transition: all 0.2s ease;
}

.preview-step-header {
  display: flex;
  align-items: center;
  padding: 10px;
  cursor: pointer;
  gap: 10px;
}

.preview-step-index {
  width: 24px;
  height: 24px;
  background: var(--action-color);
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 600;
  color: #fff;
  flex-shrink: 0;
}

.preview-step-title {
  flex: 1;
  font-size: 13px;
  color: #303133;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.expand-icon {
    font-size: 12px;
    color: #909399;
    transition: transform 0.2s;
}
.expand-icon.expanded {
    transform: rotate(-90deg);
}

.preview-step-body {
  padding: 0 10px 10px 10px;
  border-top: 1px solid #ebeef5;
  padding-top: 10px;
  margin-top: 4px;
  font-size: 12px;
}

.form-row {
  display: flex;
  margin-bottom: 6px;
  line-height: 1.4;
}
.form-row label {
    width: 60px;
    color: #909399;
    flex-shrink: 0;
}
.form-row span {
    color: #303133;
    flex: 1;
}
.break-text {
    word-break: break-all;
}

/* Transition */
.expand-enter-active,
.expand-leave-active {
  transition: all 0.2s ease;
  max-height: 300px;
  opacity: 1;
}

.expand-enter-from,
.expand-leave-to {
  max-height: 0;
  opacity: 0;
}
</style>
