<script setup>
import { ref, computed, onMounted } from 'vue'
import { Plus, Delete, Rank, ArrowDown, ArrowUp, Edit, VideoPlay, MagicStick, Crop } from '@element-plus/icons-vue'
import { useCaseStore } from '@/stores/useCaseStore'
import { storeToRefs } from 'pinia'
import { VueDraggable } from 'vue-draggable-plus'
import apiClient from '@/api'

const caseStore = useCaseStore()
const { currentCase } = storeToRefs(caseStore)

const props = defineProps({
  envId: {
    type: [Number, String],
    default: null
  }
})

// AI 生成相关状态
const aiPrompt = ref('')
const aiLoading = ref(false)
const aiDialogVisible = ref(false)

// AI 示例提示
const aiExamples = [
  '点击登录按钮，输入用户名 admin 和密码 123456，然后点击提交',
  '启动应用，等待首页加载完成，下滑列表，点击第一个商品',
  '输入搜索关键词"iPhone"，点击搜索按钮，等待结果加载',
  '强制等待 3 秒，点击返回按钮，等待后回到主页'
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

      // 直接将生成的步骤 push 到 currentCase.steps
      currentCase.value.steps.push(...newSteps)

      ElMessage.success(`已生成 ${newSteps.length} 个测试步骤`)

      // 如果有消息提示（如 Mock 数据）
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
const fetchGlobalVarKeys = async () => {
  try {
    const { data: envs } = await apiClient.getEnvironments()
    const allKeys = []
    for (const env of envs) {
      const { data: vars } = await apiClient.getVariables(env.id)
      for (const v of vars) {
        if (!allKeys.includes(v.key)) allKeys.push(v.key)
      }
    }
    globalVarKeys.value = allKeys
  } catch { /* ignore */ }
}

const appendVariable = (step, field, key) => {
  const placeholder = `{{ ${key} }}`
  step[field] = (step[field] || '') + placeholder
}

/** 变量占位符显示，避免模板里写 {{ 导致解析错误 */
const varPlaceholder = (k) => `{{ ${k} }}`

onMounted(() => {
  fetchGlobalVarKeys()
  currentCase.value?.steps?.forEach((step) => {
    if (step.action === 'extract_by_ocr') {
      ensureStepOptions(step)
    }
  })
})

const expandedSteps = ref(new Set())

const actionOptions = [
  { value: 'click', label: '点击' },
  { value: 'input', label: '输入' },
  { value: 'wait_until_exists', label: '等待元素' },
  { value: 'assert_text', label: '断言文本' },
  { value: 'swipe', label: '滑动' },
  { value: 'sleep', label: '等待' },
  { value: 'extract_by_ocr', label: 'OCR提取变量' }
]

const selectorTypeOptions = [
  { value: 'resourceId', label: 'Resource ID' },
  { value: 'text', label: 'Text' },
  { value: 'description', label: 'Description' },
  { value: 'xpath', label: 'XPath' }
]

const toggleExpand = (index) => {
  const step = currentCase.value.steps[index]
  if (step && step.action === 'extract_by_ocr') {
    ensureStepOptions(step)
  }
  if (expandedSteps.value.has(index)) {
    expandedSteps.value.delete(index)
  } else {
    expandedSteps.value.add(index)
  }
}

const isExpanded = (index) => expandedSteps.value.has(index)

const ensureStepOptions = (step) => {
  if (!step.options || typeof step.options !== 'object') {
    step.options = {}
  }
  if (!step.options.extract_rule) {
    step.options.extract_rule = 'preset'
  }
  return step.options
}

const handleActionChange = (step) => {
  if (step.action === 'extract_by_ocr') {
    ensureStepOptions(step)
  }
}

const getStepTitle = (step) => {
  const actionLabel = actionOptions.find(a => a.value === step.action)?.label || step.action
  const target = step.selector ? (step.selector.length > 25 ? step.selector.slice(0, 25) + '...' : step.selector) : '?'
  return `${actionLabel.split(' ')[0]} → ${target}`
}

const getActionColor = (action) => {
  const colors = {
    click: '#667eea',
    input: '#f093fb',
    wait_until_exists: '#4facfe',
    assert_text: '#fa709a',
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
    uuid: crypto.randomUUID(),
    action: 'click',
    selector: '',
    selector_type: 'text',
    value: '',
    options: {},
    description: 'New Step',
    error_strategy: 'ABORT'
  })
}

// Single Step Execution
import api from '@/api'
import { ElMessage } from 'element-plus'

const executingStepId = ref(null)
const emit = defineEmits(['refresh-needed', 'request-ocr-crop'])

const requestOcrCrop = (step) => {
  emit('request-ocr-crop', step)
}

const handleExecuteStep = async (step) => {
  if (executingStepId.value) return
  executingStepId.value = step
  
  const payload = {
    step,
    case_id: currentCase.value.id || null,
    env_id: props.envId,
    variables: currentCase.value.variables || []
  }
  console.log('Executing step payload:', JSON.parse(JSON.stringify(payload)))
  try {
    const res = await api.executeStep(payload)
    if (res.data.result.success) {
      ElMessage.success('步骤执行成功')
      emit('refresh-needed', res.data.dump)
    } else {
      ElMessage.error('步骤执行失败: ' + res.data.result.error)
    }
  } catch (err) {
    ElMessage.error('执行出错: ' + err.message)
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
          placeholder="例如：点击登录按钮，输入用户名 admin 和密码 123456，然后点击提交"
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
    
    <div class="step-list">
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
                <el-select v-model="step.action" size="small" @change="handleActionChange(step)">
                  <el-option 
                    v-for="opt in actionOptions" 
                    :key="opt.value" 
                    :label="opt.label" 
                    :value="opt.value" 
                  />
                </el-select>
              </div>

              <div class="form-row">
                <label>定位方式</label>
                <el-select v-model="step.selector_type" size="small" :disabled="['swipe', 'sleep', 'extract_by_ocr'].includes(step.action)">
                  <el-option
                    v-for="opt in selectorTypeOptions"
                    :key="opt.value"
                    :label="opt.label"
                    :value="opt.value"
                  />
                </el-select>
              </div>

              <div class="form-row" v-if="step.action !== 'swipe' && step.action !== 'sleep' && step.action !== 'extract_by_ocr'">
                <label>选择器</label>
                <el-input v-model="step.selector" size="small" :placeholder="step.action === 'input' ? '输入选择器（留空则在当前焦点处输入）' : '输入选择器值'">
                  <template #append v-if="globalVarKeys.length">
                    <el-dropdown trigger="click" @command="(key) => appendVariable(step, 'selector', key)">
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

              <template v-if="step.action === 'extract_by_ocr'">
                <div class="form-row">
                  <label>截取区域</label>
                  <div class="ocr-crop-input-group">
                    <el-input v-model="step.selector" size="small" placeholder="例如: [0.1, 0.2, 0.5, 0.3]">
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
                <el-input v-else v-model="step.value" size="small" placeholder="输入值或引用 {{ VAR }}">
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
