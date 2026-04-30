<script setup>
import { ref, onMounted, watch, nextTick } from 'vue'
import { Plus, Delete, Rank, ArrowDown, VideoPlay, MagicStick, Crop } from '@element-plus/icons-vue'
import { useCaseStore } from '@/stores/useCaseStore'
import { storeToRefs } from 'pinia'
import { VueDraggable } from 'vue-draggable-plus'
import apiClient from '@/api'
import api from '@/api'
import { ElMessage } from 'element-plus'
import { createUuid } from '@/utils/uuid'

const caseStore = useCaseStore()
const { currentCase, lastAddedStepUuid } = storeToRefs(caseStore)

const props = defineProps({
  envId: {
    type: [Number, String],
    default: null
  },
  deviceSerial: {
    type: String,
    default: ''
  },
  activeImageCropStepUuid: {
    type: String,
    default: ''
  }
})

const logicalLocatorActions = ['click', 'input', 'wait_until_exists']
const byOptions = [
  { value: 'text', label: 'Text' },
  { value: 'description', label: 'Description' }
]
const assertTextMatchOptions = [
  { value: 'contains', label: '包含' },
  { value: 'not_contains', label: '不包含' }
]
const assertImageMatchOptions = [
  { value: 'exists', label: '存在' },
  { value: 'not_exists', label: '不存在' }
]
const selectorTypeToBy = {
  text: 'text',
  description: 'description',
  image: 'image'
}
const byToSelectorType = {
  text: 'text',
  description: 'description',
  desc: 'description'
}

const isLogicalLocatorAction = (action) => logicalLocatorActions.includes(String(action || '').toLowerCase())
const crossPlatformActions = ['click', 'input', 'wait_until_exists', 'assert_text', 'assert_image', 'swipe', 'sleep', 'start_app', 'stop_app', 'back', 'home', 'click_image', 'extract_by_ocr']
const autoExecuteOnByAction = (action) => (crossPlatformActions.includes(String(action || '').toLowerCase()) ? ['android', 'ios'] : ['android'])

function ensureStepOptions(step) {
  if (!step.options || typeof step.options !== 'object') {
    step.options = {}
  }
  if (!step.options.extract_rule) {
    step.options.extract_rule = 'preset'
  }
  return step.options
}

function ensureAssertTextOptions(step) {
  if (!step.options || typeof step.options !== 'object') {
    step.options = {}
  }
  if (!step.options.match_mode) {
    step.options.match_mode = 'contains'
  }
  return step.options
}

function ensureAssertImageOptions(step) {
  if (!step.options || typeof step.options !== 'object') {
    step.options = {}
  }
  if (!step.options.match_mode) {
    step.options.match_mode = 'exists'
  }
  return step.options
}

const ensureCrossPlatformConfig = (step) => {
  if (!step || typeof step !== 'object') return

  const executeOn = autoExecuteOnByAction(step.action)
  if (JSON.stringify(step.execute_on || []) !== JSON.stringify(executeOn)) {
    step.execute_on = executeOn
  }

  if (!step.platform_overrides || typeof step.platform_overrides !== 'object') {
    step.platform_overrides = {}
  }

  const normalizeOverride = (candidate) => {
    if (!candidate || typeof candidate !== 'object') return null
    const selector = String(candidate.selector || '').trim()
    const by = String(candidate.by || '').trim().toLowerCase()
    if (!by) return null
    return { selector, by }
  }

  let androidOverride = normalizeOverride(step.platform_overrides.android)
  if (!androidOverride) {
    const selector = String(step.selector || '').trim()
    const selectorType = String(step.selector_type || '').trim()
    const by = selectorTypeToBy[selectorType]
    if (selector && by) {
      androidOverride = { selector, by }
    }
  }

  const sameOverride = (a, b) => {
    if (!a && !b) return true
    if (!a || !b) return false
    return String(a.selector || '') === String(b.selector || '')
      && String(a.by || '') === String(b.by || '')
  }

  if (!sameOverride(step.platform_overrides.android, androidOverride)) {
    step.platform_overrides.android = androidOverride
  }

  if (!step.error_strategy) {
    step.error_strategy = 'ABORT'
  }

  if (!step.timeout || Number(step.timeout) < 1) {
    step.timeout = 10
  }

  if (step.action === 'assert_text') {
    step.selector = ''
    step.selector_type = null
    step.platform_overrides.android = null
    step.platform_overrides.ios = null
    ensureAssertTextOptions(step)
  } else if (step.action === 'click_image' || step.action === 'assert_image') {
    step.selector_type = 'image'
  } else if (isLogicalLocatorAction(step.action)) {
    const selectorType = String(step.selector_type || '').trim()
    if (!['text', 'description'].includes(selectorType)) {
      step.selector_type = 'text'
    }
  }

  if (step.action === 'assert_image') {
    ensureAssertImageOptions(step)
  } else if (step.action === 'extract_by_ocr') {
    ensureStepOptions(step)
  }
}

const getPlatformOverride = (step, platform) => {
  ensureCrossPlatformConfig(step)
  const override = step.platform_overrides?.[platform]
  if (override && typeof override === 'object') {
    return override
  }

  const fallbackBy = 'text'
  if (!step.platform_overrides || typeof step.platform_overrides !== 'object') {
    step.platform_overrides = {}
  }
  step.platform_overrides[platform] = {
    selector: '',
    by: fallbackBy
  }
  return step.platform_overrides[platform]
}

const syncLegacyFromAndroidOverride = (step) => {
  const android = getPlatformOverride(step, 'android')
  step.selector = android.selector || ''
  const mappedSelectorType = byToSelectorType[String(android.by || '').toLowerCase()]
  if (mappedSelectorType) {
    step.selector_type = mappedSelectorType
  }
}

const syncNonLogicalSelectorToAndroidOverride = (step) => {
  if (!step || typeof step !== 'object') return
  ensureCrossPlatformConfig(step)
  if (!step.platform_overrides || typeof step.platform_overrides !== 'object') {
    step.platform_overrides = {}
  }
  const selector = String(step.selector || '').trim()
  const selectorType = String(step.selector_type || '').trim()
  const by = selectorTypeToBy[selectorType] || 'text'
  step.platform_overrides.android = { selector, by }
}

const syncExtractOcrRegion = (step) => {
  if (!step || typeof step !== 'object') return
  step.selector_type = 'text'
  syncNonLogicalSelectorToAndroidOverride(step)
}

// AI 生成相关状态
const aiPrompt = ref('')
const aiLoading = ref(false)
const aiDialogVisible = ref(false)

// AI 示例提示
const aiExamples = [
  '点击登录按钮，输入用户名 admin 和密码 123456，然后点击提交',
  '启动应用，等待首页加载完成，下滑列表，点击第一个商品，文本断言包含“推荐商品”',
  '输入搜索关键词"iPhone"，点击搜索按钮，等待结果加载',
  '强制等待 3 秒，点击返回按钮，文本断言不包含“加载中”'
]

const showAIDialog = () => {
  aiDialogVisible.value = true
}

const generateStepsFromAI = async () => {
  if (!aiPrompt.value.trim()) {
    ElMessage.warning('请输入测试步骤描述')
    return
  }

  aiLoading.value = true

  try {
    const res = await apiClient.generateAISteps(aiPrompt.value)

    if (res.data.success) {
      const newSteps = res.data.data

      newSteps.forEach((step) => caseStore.addStep(step))

      ElMessage.success(`已生成 ${newSteps.length} 个测试步骤`)

      // 后端可能返回降级提示（如使用兜底模板步骤）
      if (res.data.message) {
        setTimeout(() => {
          ElMessage.info(res.data.message)
        }, 1000)
      }

      // 清空输入框并关闭弹窗
      aiPrompt.value = ''
      aiDialogVisible.value = false
    } else {
      ElMessage.error(`生成失败: ${res.data.message}`)
    }
  } catch (err) {
    console.error('AI 生成步骤失败:', err)
    ElMessage.error('AI 生成步骤失败，请检查网络连接或联系管理员')
  } finally {
    aiLoading.value = false
  }
}
// 全局变量引用
const globalVarKeys = ref([])
const normalizeEnvId = (value) => {
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null
}

const fetchGlobalVarKeys = async (envId = props.envId) => {
  const normalizedEnvId = normalizeEnvId(envId)
  if (!normalizedEnvId) {
    globalVarKeys.value = []
    return
  }

  try {
    const { data: vars } = await apiClient.getVariables(normalizedEnvId)
    globalVarKeys.value = Array.from(
      new Set(
        (Array.isArray(vars) ? vars : [])
          .map((item) => String(item?.key || '').trim())
          .filter(Boolean)
      )
    )
  } catch {
    globalVarKeys.value = []
  }
}

const appendVariable = (step, field, key, sourceStep = null) => {
  const placeholder = `{{ ${key} }}`
  step[field] = (step[field] || '') + placeholder
  if (!sourceStep || field !== 'selector') return
  if (isLogicalLocatorAction(sourceStep.action)) {
    syncLegacyFromAndroidOverride(sourceStep)
    return
  }
  if (!['swipe', 'sleep'].includes(String(sourceStep.action || '').toLowerCase())) {
    syncNonLogicalSelectorToAndroidOverride(sourceStep)
  }
}

/** 变量占位符显示，避免模板里写 {{ 导致解析错误 */
const varPlaceholder = (k) => `{{ ${k} }}`

onMounted(() => {
  fetchGlobalVarKeys()
  currentCase.value?.steps?.forEach((step) => {
    ensureCrossPlatformConfig(step)
  })
})

watch(
  () => props.envId,
  (envId) => {
    fetchGlobalVarKeys(envId)
  }
)

watch(
  () => currentCase.value.steps,
  (steps) => {
    ;(steps || []).forEach((step) => ensureCrossPlatformConfig(step))
  },
  { deep: true, immediate: true }
)

const expandedSteps = ref(new Set())
const stepListRef = ref(null)
const SCROLL_PADDING = 16
const EXPAND_SCROLL_DELAY = 260

const getStepIndexByUuid = (stepUuid) => {
  if (!stepUuid) return -1
  return currentCase.value.steps.findIndex((step) => step?.uuid === stepUuid)
}

const getStepCardElement = (index) => {
  if (index < 0 || !stepListRef.value) return null
  return stepListRef.value.querySelector(`[data-step-index="${index}"]`)
}

const scrollStepIntoView = async (stepUuid, { revealBottom = false } = {}) => {
  if (!stepUuid) return

  await nextTick()

  const container = stepListRef.value
  const stepIndex = getStepIndexByUuid(stepUuid)
  const stepCard = getStepCardElement(stepIndex)
  if (!container || !stepCard) return

  const top = Math.max(0, stepCard.offsetTop - SCROLL_PADDING)
  const bottom = stepCard.offsetTop + stepCard.offsetHeight + SCROLL_PADDING
  const visibleTop = container.scrollTop
  const visibleBottom = visibleTop + container.clientHeight

  let targetTop = null
  if (revealBottom) {
    targetTop = Math.max(0, bottom - container.clientHeight)
  } else if (bottom > visibleBottom) {
    targetTop = Math.max(0, bottom - container.clientHeight)
  } else if (top < visibleTop) {
    targetTop = top
  }

  if (targetTop !== null) {
    container.scrollTo({
      top: targetTop,
      behavior: 'smooth'
    })
  }
}

const syncLatestExpandedStepVisibility = (stepUuid) => {
  if (!stepUuid) return
  scrollStepIntoView(stepUuid, { revealBottom: true })
  window.setTimeout(() => {
    if (lastAddedStepUuid.value === stepUuid) {
      scrollStepIntoView(stepUuid, { revealBottom: true })
    }
  }, EXPAND_SCROLL_DELAY)
}

watch(lastAddedStepUuid, (stepUuid, previousUuid) => {
  if (!stepUuid || stepUuid === previousUuid) return
  scrollStepIntoView(stepUuid)
})

const actionOptions = [
  { value: 'input', label: '输入' },
  { value: 'wait_until_exists', label: '等待元素' },
  { value: 'assert_text', label: '文本断言' },
  { value: 'assert_image', label: '图像断言' },
  { value: 'sleep', label: '强制等待' },
  { value: 'swipe', label: '滑动' },
  { value: 'extract_by_ocr', label: 'OCR提取变量' },
  { value: 'click_image', label: '图像点击' },
  { value: 'click', label: '点击' }
]

const toggleExpand = (index) => {
  const step = currentCase.value.steps[index]
  const stepUuid = step?.uuid || null
  const willExpand = !expandedSteps.value.has(index)
  if (step && step.action === 'extract_by_ocr') {
    ensureStepOptions(step)
  }
  if (expandedSteps.value.has(index)) {
    expandedSteps.value.delete(index)
  } else {
    expandedSteps.value.add(index)
  }
  if (willExpand && stepUuid && stepUuid === lastAddedStepUuid.value) {
    syncLatestExpandedStepVisibility(stepUuid)
  }
}

const isExpanded = (index) => expandedSteps.value.has(index)

const resetStepDetailsForAction = (step) => {
  if (!step || typeof step !== 'object') return

  step.selector = ''
  step.selector_type = ''
  step.value = ''
  step.description = ''
  step.timeout = 10
  step.error_strategy = 'ABORT'
  step.options = {}
  step.platform_overrides = {
    android: null,
    ios: null
  }
}

const handleActionChange = (step) => {
  resetStepDetailsForAction(step)
  ensureCrossPlatformConfig(step)
  if (step.action === 'sleep') {
    const seconds = Number.parseFloat(step.value)
    if (!Number.isFinite(seconds) || seconds < 1) {
      step.value = '5'
    }
  }
  if (step.action === 'assert_text') {
    ensureAssertTextOptions(step)
  }
  if (step.action === 'assert_image') {
    ensureAssertImageOptions(step)
  }
  if (step.action === 'extract_by_ocr') {
    ensureStepOptions(step)
  }
}

const getStepTitle = (step) => {
  const actionLabel = actionOptions.find(a => a.value === step.action)?.label || step.action
  if (step.action === 'assert_text') {
    const matchMode = step?.options?.match_mode === 'not_contains' ? '不包含' : '包含'
    const expectedText = String(step?.value || '').trim()
    const target = expectedText ? (expectedText.length > 25 ? expectedText.slice(0, 25) + '...' : expectedText) : '?'
    return `${actionLabel}${matchMode} → ${target}`
  }
  if (step.action === 'assert_image') {
    const matchMode = step?.options?.match_mode === 'not_exists' ? '不存在' : '存在'
    const imagePath = String(step?.selector || '').trim()
    const target = imagePath ? (imagePath.length > 25 ? imagePath.slice(0, 25) + '...' : imagePath) : '?'
    return `${actionLabel}${matchMode} → ${target}`
  }
  const action = String(step?.action || '').toLowerCase()
  const targetSelector = isLogicalLocatorAction(action)
    ? (step?.platform_overrides?.android?.selector || step.selector)
    : (step?.selector || step?.platform_overrides?.android?.selector)
  const target = targetSelector ? (targetSelector.length > 25 ? targetSelector.slice(0, 25) + '...' : targetSelector) : '?'
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
    extract_by_ocr: '#67c23a'
  }
  return colors[action] || '#909399'
}

const removeStep = (index) => {
  caseStore.removeStep(index)
}

const addCustomStep = () => {
  caseStore.addStep({
    uuid: createUuid(),
    action: 'assert_text',
    selector: '',
    selector_type: '',
    value: '',
    options: {},
    description: '文本断言',
    error_strategy: 'ABORT',
    execute_on: ['android', 'ios'],
    platform_overrides: {
      android: null
    }
  })
}

const executingStepId = ref(null)
const emit = defineEmits(['refresh-needed', 'request-ocr-crop', 'request-image-crop'])

const requestOcrCrop = (step) => {
  emit('request-ocr-crop', step)
}

const requestImageCrop = (step) => {
  emit('request-image-crop', step)
}

const isImageCropActive = (step) => {
  const stepUuid = String(step?.uuid || '').trim()
  return Boolean(stepUuid) && stepUuid === props.activeImageCropStepUuid
}

const handleExecuteStep = async (step) => {
  if (executingStepId.value) return
  if (!props.deviceSerial) {
    ElMessage.warning('请先选择一台调试设备')
    return
  }
  executingStepId.value = step

  ensureCrossPlatformConfig(step)
  if (isLogicalLocatorAction(step.action)) {
    syncLegacyFromAndroidOverride(step)
  }
  
  const payload = {
    step,
    case_id: currentCase.value.id || null,
    env_id: props.envId,
    variables: currentCase.value.variables || [],
    device_serial: props.deviceSerial
  }
  try {
    const res = await api.executeStep(payload)
    if (res.data.result.success) {
      ElMessage.success('步骤执行成功')
      emit('refresh-needed', res.data.dump)
    } else {
      ElMessage.error('步骤执行失败: ' + res.data.result.error)
    }
  } catch (err) {
    ElMessage.error('执行出错: ' + (err.response?.data?.detail || err.message))
  } finally {
    executingStepId.value = null
  }
}

</script>

<template>
  <div class="step-builder">
    <div class="builder-header">
      <div class="header-left">
        <span class="title">步骤编排</span>
        <span class="step-count">{{ currentCase.steps.length }} 步</span>
      </div>
      <div class="header-right">
        <slot name="header-actions"></slot>
      </div>
    </div>

    <!-- AI 生成触发按钮 -->
    <div class="ai-trigger-section">
      <el-button
        :icon="MagicStick"
        @click="showAIDialog"
        :loading="aiLoading"
        type="primary"
        plain
        style="width: 100%"
      >
        AI 智能生成测试步骤
      </el-button>
    </div>

    <!-- AI 生成弹窗 -->
    <el-dialog
      v-model="aiDialogVisible"
      title="AI 智能生成测试步骤"
      width="600px"
      :close-on-click-modal="true"
      :close-on-press-escape="true"
    >
      <div class="ai-dialog-content">
        <div class="ai-description">
          <el-icon><MagicStick /></el-icon>
          <span>用自然语言描述你的测试步骤，AI 将自动生成可执行的测试脚本</span>
        </div>

        <el-input
          v-model="aiPrompt"
          type="textarea"
          :rows="6"
          placeholder="例如：点击登录按钮，输入用户名 admin 和密码 123456，然后点击提交，文本断言包含“登录成功”"
          resize="none"
        />

        <div class="ai-examples">
          <div class="examples-title">示例：</div>
          <div class="examples-list">
            <el-tag
              v-for="(example, index) in aiExamples"
              :key="index"
              @click="aiPrompt = example"
              class="example-tag"
            >
              {{ example }}
            </el-tag>
          </div>
        </div>
      </div>

      <template #footer>
        <div class="ai-dialog-footer">
          <el-button @click="aiDialogVisible = false">取消</el-button>
          <el-button
            type="primary"
            @click="generateStepsFromAI"
            :loading="aiLoading"
            :icon="MagicStick"
          >
            生成步骤
          </el-button>
        </div>
      </template>
    </el-dialog>
    
    <div ref="stepListRef" class="step-list">
      <VueDraggable
        v-model="currentCase.steps"
        :animation="200"
        handle=".drag-handle"
        group="steps"
        class="draggable-container"
      >
        <div
          v-for="(step, index) in currentCase.steps"
          :key="step.uuid || index"
          class="step-card"
          :data-step-index="index"
          :style="{ '--action-color': getActionColor(step.action) }"
        >
          <div class="step-header" @click="toggleExpand(index)">
            <div class="drag-handle">
              <el-icon><Rank /></el-icon>
            </div>
            <div class="step-index">{{ index + 1 }}</div>
            <div class="step-title">{{ getStepTitle(step) }}</div>
            <div class="step-actions">
              <el-button 
                :icon="VideoPlay" 
                size="small" 
                circle
                type="success" 
                @click.stop="handleExecuteStep(step)"
                :loading="executingStepId === step" 
                title="执行步骤"
              />
              <el-button :icon="Delete" size="small" circle type="danger" @click.stop="removeStep(index)" title="删除步骤" />
              <el-icon class="expand-icon" :class="{ expanded: isExpanded(index) }">
                <ArrowDown />
              </el-icon>
            </div>
          </div>
          
          <transition name="expand">
            <div v-if="isExpanded(index)" class="step-body">
              <div class="form-row">
                <label>动作</label>
                <el-select
                  v-model="step.action"
                  size="small"
                  popper-class="step-action-select-dropdown"
                  @change="handleActionChange(step)"
                >
                  <el-option 
                    v-for="opt in actionOptions" 
                    :key="opt.value" 
                    :label="opt.label" 
                    :value="opt.value" 
                  />
                </el-select>
              </div>

              <div class="form-row" v-if="step.action === 'assert_text'">
                <label>断言方式</label>
                <el-select v-model="step.options.match_mode" size="small">
                  <el-option
                    v-for="opt in assertTextMatchOptions"
                    :key="opt.value"
                    :label="opt.label"
                    :value="opt.value"
                  />
                </el-select>
              </div>

              <div class="form-row" v-if="step.action === 'assert_image'">
                <label>断言方式</label>
                <el-select v-model="step.options.match_mode" size="small">
                  <el-option
                    v-for="opt in assertImageMatchOptions"
                    :key="opt.value"
                    :label="opt.label"
                    :value="opt.value"
                  />
                </el-select>
              </div>

              <template v-if="isLogicalLocatorAction(step.action)">
                <div class="form-row">
                  <label>定位语义</label>
                  <el-select
                    v-model="getPlatformOverride(step, 'android').by"
                    size="small"
                    @change="syncLegacyFromAndroidOverride(step)"
                  >
                    <el-option
                      v-for="opt in byOptions"
                      :key="opt.value"
                      :label="opt.label"
                      :value="opt.value"
                    />
                  </el-select>
                </div>

                <div class="form-row">
                  <label>{{ step.action === 'input' ? '定位值（可选）' : '定位值' }}</label>
                  <el-input
                    v-model="getPlatformOverride(step, 'android').selector"
                    size="small"
                    :placeholder="step.action === 'input' ? '可留空：留空时向当前焦点输入框输入' : '输入定位值'"
                    @input="syncLegacyFromAndroidOverride(step)"
                  >
                    <template #append v-if="globalVarKeys.length">
                      <el-dropdown trigger="click" @command="(key) => appendVariable(getPlatformOverride(step, 'android'), 'selector', key, step)">
                        <el-button size="small" class="var-dropdown-btn">{{ '{ }' }}</el-button>
                        <template #dropdown>
                          <el-dropdown-menu>
                            <el-dropdown-item v-for="k in globalVarKeys" :key="k" :command="k">
                              {{ varPlaceholder(k) }}
                            </el-dropdown-item>
                          </el-dropdown-menu>
                        </template>
                      </el-dropdown>
                    </template>
                  </el-input>
                </div>
              </template>

              <div class="form-row" v-if="!isLogicalLocatorAction(step.action) && step.action !== 'assert_text' && step.action !== 'assert_image' && step.action !== 'swipe' && step.action !== 'sleep' && step.action !== 'extract_by_ocr' && step.action !== 'click_image'">
                <label>参数</label>
                <el-input
                  v-model="step.selector"
                  size="small"
                  placeholder="输入参数值"
                  @input="syncNonLogicalSelectorToAndroidOverride(step)"
                >
                  <template #append v-if="globalVarKeys.length">
                    <el-dropdown trigger="click" @command="(key) => appendVariable(step, 'selector', key, step)">
                      <el-button size="small" class="var-dropdown-btn">{{ '{ }' }}</el-button>
                      <template #dropdown>
                        <el-dropdown-menu>
                          <el-dropdown-item v-for="k in globalVarKeys" :key="k" :command="k">
                            {{ varPlaceholder(k) }}
                          </el-dropdown-item>
                        </el-dropdown-menu>
                      </template>
                    </el-dropdown>
                  </template>
                </el-input>
              </div>

              <template v-if="step.action === 'click_image' || step.action === 'assert_image'">
                <div class="form-row">
                  <label>模板图路径</label>
                  <div class="ocr-crop-input-group">
                    <el-input
                      v-model="step.selector"
                      size="small"
                      placeholder="例如: static/images/element_xxx.png"
                      @input="syncNonLogicalSelectorToAndroidOverride(step)"
                    >
                      <template #prepend>图像路径</template>
                    </el-input>
                    <el-button
                      size="small"
                      type="primary"
                      :icon="Crop"
                      @click.stop="requestImageCrop(step)"
                      class="ocr-crop-btn"
                    >
                      {{ isImageCropActive(step) ? '取消截取' : '去截取' }}
                    </el-button>
                  </div>
                </div>
              </template>

              <template v-if="step.action === 'extract_by_ocr'">
                <div class="form-row">
                  <label>截取区域</label>
                  <div class="ocr-crop-input-group">
                    <el-input
                      v-model="step.selector"
                      size="small"
                      placeholder="例如: [0.1, 0.2, 0.5, 0.3]"
                      @input="syncExtractOcrRegion(step)"
                    >
                      <template #prepend>百分比区域</template>
                    </el-input>
                    <el-button
                      size="small"
                      type="primary"
                      :icon="Crop"
                      @click.stop="requestOcrCrop(step)"
                      class="ocr-crop-btn"
                    >
                      去框选
                    </el-button>
                  </div>
                </div>

                <div class="form-row">
                  <label>存入变量名</label>
                  <el-input v-model="step.value" size="small" placeholder="例如: ORDER_ID" />
                </div>

                <div class="form-row">
                  <label>提取规则</label>
                  <el-radio-group v-model="step.options.extract_rule" size="small">
                    <el-radio label="preset">内置模板</el-radio>
                    <el-radio label="boundary">掐头去尾</el-radio>
                    <el-radio label="regex">高级正则</el-radio>
                  </el-radio-group>
                </div>

                <div class="form-row" v-if="step.options.extract_rule === 'preset'">
                  <label>模板类型</label>
                  <el-select v-model="step.options.preset_type" size="small" placeholder="选择模板类型">
                    <el-option label="纯数字" value="number_only" />
                    <el-option label="价格" value="price" />
                    <el-option label="字母+数字" value="alphanumeric" />
                    <el-option label="全部中文" value="chinese" />
                  </el-select>
                </div>

                <div class="form-row" v-if="step.options.extract_rule === 'boundary'">
                  <label>边界字符</label>
                  <div class="boundary-inputs">
                    <el-input v-model="step.options.left_bound" size="small" placeholder="左边界" />
                    <el-input v-model="step.options.right_bound" size="small" placeholder="右边界" />
                  </div>
                </div>

                <div class="form-row" v-if="step.options.extract_rule === 'regex'">
                  <label>正则表达式</label>
                  <el-input v-model="step.options.custom_regex" size="small" placeholder="请输入正则表达式" />
                </div>
              </template>

              <div class="form-row" v-if="step.action === 'swipe'">
                <label>方向</label>
                <el-select v-model="step.selector" size="small" placeholder="选择滑动方向">
                  <el-option value="up" label="上滑 (Up)" />
                  <el-option value="down" label="下滑 (Down)" />
                  <el-option value="left" label="左滑 (Left)" />
                  <el-option value="right" label="右滑 (Right)" />
                </el-select>
              </div>
              
              <div class="form-row" v-if="['input', 'assert_text', 'sleep'].includes(step.action)">
                <label v-if="step.action === 'sleep'">等待时间(秒)</label>
                <label v-else-if="step.action === 'assert_text'">文本内容</label>
                <label v-else>值</label>

                <el-input-number
                  v-if="step.action === 'sleep'"
                  :model-value="parseFloat(step.value) || 5"
                  @update:model-value="val => step.value = String(val)"
                  :min="1"
                  :max="120"
                  :step="1"
                  controls-position="right"
                  size="small"
                  style="flex: 1"
                  class="sleep-input-number"
                />
                <el-input
                  v-else
                  v-model="step.value"
                  size="small"
                  :placeholder="step.action === 'assert_text' ? '输入要在当前页面中判断的文本或引用 {{ VAR }}' : '输入值或引用 {{ VAR }}'"
                >
                  <template #append v-if="globalVarKeys.length">
                    <el-dropdown trigger="click" @command="(key) => appendVariable(step, 'value', key)">
                      <el-button size="small" class="var-dropdown-btn">{{ '{ }' }}</el-button>
                      <template #dropdown>
                        <el-dropdown-menu>
                          <el-dropdown-item v-for="k in globalVarKeys" :key="k" :command="k">
                            {{ varPlaceholder(k) }}
                          </el-dropdown-item>
                        </el-dropdown-menu>
                      </template>
                    </el-dropdown>
                  </template>
                </el-input>
              </div>
              
              <div class="form-row">
                <label>容错策略</label>
                <div class="strategy-group">
                  <el-select v-model="step.error_strategy" size="small" placeholder="请选择容错策略">
                    <el-option label="立即终止" value="ABORT" />
                    <el-option label="失败但继续" value="CONTINUE" />
                    <el-option label="忽略错误" value="IGNORE" />
                  </el-select>
                </div>
              </div>
              
              <div class="form-row">
                <label>描述</label>
                <el-input v-model="step.description" size="small" placeholder="步骤描述（可选）" />
              </div>
            </div>
          </transition>
        </div>
      </VueDraggable>
      
      <el-empty v-if="currentCase.steps.length === 0" description="暂无步骤，点击设备画布开始录制" :image-size="60" />
    </div>
    
    <div class="builder-footer">
      <el-button :icon="Plus" type="primary" @click="addCustomStep" style="width: 100%">
        添加自定义步骤
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.step-builder {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #fff;
  border-left: 1px solid #e4e7ed;
}

.builder-header {
  padding: 12px 20px;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  justify-content: space-between;
  align-items: center;
  background-color: #fafafa;
  flex-shrink: 0;
  box-sizing: border-box;
  height: 50px;
}

.header-left {
  display: flex;
  align-items: center;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 8px; /* Button gap */
}

.title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

.step-count {
  font-size: 12px;
  color: #409eff;
  background: #ecf5ff;
  padding: 4px 10px;
  border-radius: 12px;
}

.ai-trigger-section {
  padding: 12px;
  border-bottom: 1px solid #e4e7ed;
}

.ai-dialog-content {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.ai-description {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  background: linear-gradient(135deg, #f0f4ff 0%, #e8f0fe 100%);
  border-radius: 8px;
  border-left: 3px solid #667eea;
  color: #409eff;
  font-size: 14px;
  line-height: 1.6;
}

.ai-description .el-icon {
  font-size: 18px;
  flex-shrink: 0;
}

.ai-examples {
  margin-top: 8px;
}

.examples-title {
  font-size: 13px;
  color: #606266;
  margin-bottom: 8px;
}

.examples-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.example-tag {
  cursor: pointer;
  transition: all 0.2s;
}

.example-tag:hover {
  background: #667eea;
  color: #fff;
  transform: translateY(-2px);
  box-shadow: 0 2px 8px rgba(102, 126, 234, 0.3);
}

.ai-dialog-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
}

.ai-input-section {
  padding: 12px;
  border-bottom: 1px solid #e4e7ed;
  background: linear-gradient(135deg, #f5f7fa 0%, #e9ecf1 100%);
}

.step-list {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
}

.draggable-container {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.step-card {
  background: #fafafa;
  border-radius: 8px;
  border-left: 3px solid var(--action-color);
  overflow: hidden;
  transition: all 0.2s ease;
}

.step-card:hover {
  background: #f5f7fa;
}

.step-header {
  display: flex;
  align-items: center;
  padding: 12px;
  cursor: pointer;
  gap: 10px;
}

.drag-handle {
  cursor: grab;
  color: #c0c4cc;
  display: flex;
  align-items: center;
}

.drag-handle:active {
  cursor: grabbing;
}

.step-index {
  width: 28px;
  height: 28px;
  background: var(--action-color);
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 600;
  color: #fff;
  flex-shrink: 0;
}

.step-title {
  flex: 1;
  font-size: 13px;
  color: #303133;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin-right: 8px;
}

.step-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.expand-icon {
  transition: transform 0.2s ease;
  color: #909399;
}

.expand-icon.expanded {
  transform: rotate(180deg);
}

.step-body {
  padding: 0 12px 12px 12px;
  border-top: 1px solid #ebeef5;
  padding-top: 12px;
  margin-top: 4px;
}

.form-row {
  display: flex;
  align-items: center;
  margin-bottom: 10px;
  gap: 10px;
}

.form-row:last-child {
  margin-bottom: 0;
}

.form-row label {
  width: 70px;
  font-size: 12px;
  color: #606266;
  flex-shrink: 0;
}

.form-row .el-select,
.form-row .el-input {
  flex: 1;
}

.form-row .el-checkbox-group {
  flex: 1;
  display: flex;
  gap: 10px;
}

.strategy-group {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.boundary-inputs {
  flex: 1;
  display: flex;
  gap: 8px;
}

.help-text {
  font-size: 11px;
  color: #909399;
  line-height: 1.2;
}

.builder-footer {
  padding: 12px;
  border-top: 1px solid #e4e7ed;
}

.var-dropdown-btn {
  font-family: 'SF Mono', 'Menlo', 'Monaco', monospace;
  font-weight: 700;
  color: #409eff;
  padding: 0 6px;
}

.sleep-input-number :deep(.el-input__inner) {
  text-align: left;
}

/* OCR 框选按钮组样式 */
.ocr-crop-input-group {
  display: flex;
  gap: 8px;
  flex: 1;
}

.ocr-crop-input-group .el-input {
  flex: 1;
}

.ocr-crop-btn {
  flex-shrink: 0;
  white-space: nowrap;
}

:global(.step-action-select-dropdown .el-select-dropdown__wrap) {
  max-height: none !important;
}

:global(.step-action-select-dropdown .el-scrollbar__view) {
  max-height: none !important;
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
  padding-top: 0;
  padding-bottom: 0;
}
</style>
