<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'

const loading = ref(false)
const saving = ref(false)
const testingUi = ref(false)
const testingFb = ref(false)
const testingAi = ref(false)
const activeTab = ref('notification')

const form = ref({
  feishu_webhook: '',
  system_base_url: '',
  fastbot_webhook: '',
  ai_api_key: '',
  ai_api_base: '',
  ai_model: '',
})

const loadSettings = async () => {
  loading.value = true
  try {
    const res = await api.getSettings()
    const settings = res.data || []
    for (const s of settings) {
      if (s.key === 'feishu_webhook') form.value.feishu_webhook = s.value
      if (s.key === 'system_base_url') form.value.system_base_url = s.value
      if (s.key === 'fastbot_webhook') form.value.fastbot_webhook = s.value
      if (s.key === 'ai_api_key') form.value.ai_api_key = s.value
      if (s.key === 'ai_api_base') form.value.ai_api_base = s.value
      if (s.key === 'ai_model') form.value.ai_model = s.value
    }
  } catch (err) {
    console.error('加载配置失败', err)
  } finally {
    loading.value = false
  }
}

const handleSave = async () => {
  saving.value = true
  try {
    await api.saveSettings([
      { key: 'feishu_webhook', value: form.value.feishu_webhook, description: 'UI 场景报告 Webhook 地址' },
      { key: 'system_base_url', value: form.value.system_base_url, description: '系统访问基础地址' },
      { key: 'fastbot_webhook', value: form.value.fastbot_webhook, description: '智能探索报告 Webhook 地址' },
      { key: 'ai_api_key', value: form.value.ai_api_key, description: 'AI 模型 API Key' },
      { key: 'ai_api_base', value: form.value.ai_api_base, description: 'AI 模型 API 地址' },
      { key: 'ai_model', value: form.value.ai_model, description: 'AI 模型名称' },
    ])
    ElMessage.success('配置已保存')
  } catch (err) {
    ElMessage.error('保存失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    saving.value = false
  }
}

const handleTestUi = async () => {
  if (!form.value.feishu_webhook) {
    ElMessage.warning('请先填写 UI 场景报告的 Webhook 地址')
    return
  }
  testingUi.value = true
  try {
    await api.sendTestNotification(form.value.feishu_webhook)
    ElMessage.success('测试消息已发送，请检查对应群聊')
  } catch (err) {
    ElMessage.error('发送失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    testingUi.value = false
  }
}

const handleTestFb = async () => {
  if (!form.value.fastbot_webhook) {
    ElMessage.warning('请先填写智能探索报告的 Webhook 地址')
    return
  }
  testingFb.value = true
  try {
    await api.sendTestNotification(form.value.fastbot_webhook)
    ElMessage.success('测试消息已发送，请检查对应群聊')
  } catch (err) {
    ElMessage.error('发送失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    testingFb.value = false
  }
}

const handleTestAi = async () => {
  if (!form.value.ai_api_key) {
    ElMessage.warning('请先填写 API Key')
    return
  }
  testingAi.value = true
  try {
    // 使用一个简短的测试日志调用 AI 分析接口
    await api.analyzeLog({
      log_text: 'FATAL EXCEPTION: main\nProcess: com.test.app, PID: 12345\njava.lang.NullPointerException: Attempt to invoke virtual method on a null object reference\n\tat com.test.app.MainActivity.onCreate(MainActivity.java:42)',
      package_name: 'com.test.app',
      device_info: 'Test Device',
    })
    ElMessage.success('AI 模型连接测试成功！')
  } catch (err) {
    ElMessage.error('测试失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    testingAi.value = false
  }
}

onMounted(loadSettings)
</script>

<template>
  <div class="notification-settings" v-loading="loading">
    <div class="page-header">
      <h2>系统设置</h2>
      <p class="page-desc">管理通知推送和 AI 分析模型的配置。</p>
    </div>

    <!-- Tab 切换 -->
    <el-tabs v-model="activeTab" class="settings-tabs">
      <!-- 通知推送 Tab -->
      <el-tab-pane label="📢 通知推送" name="notification">
        <!-- 通知设置 双面板布局 -->
        <div class="dual-panel">
          <!-- 左侧：UI 场景报告 -->
          <el-card shadow="never" class="panel-card">
            <template #header>
              <div class="card-header"><span>UI 场景报告</span></div>
            </template>

            <el-form label-position="top">
              <el-form-item label="Webhook 地址">
                <el-input
                  v-model="form.feishu_webhook"
                  placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxx"
                  clearable
                >
                  <template #prefix><el-icon><Link /></el-icon></template>
                </el-input>
              </el-form-item>

              <el-form-item label="系统访问地址">
                <el-input
                  v-model="form.system_base_url"
                  placeholder="http://localhost:5173"
                  clearable
                >
                  <template #prefix><el-icon><Monitor /></el-icon></template>
                </el-input>
                <div class="form-tip">用于在通知卡片中生成「查看详细报告」的链接地址。</div>
              </el-form-item>

              <div class="form-actions">
                <el-button @click="handleTestUi" :loading="testingUi" :disabled="!form.feishu_webhook">发送测试消息</el-button>
              </div>
            </el-form>
          </el-card>

          <!-- 右侧：智能探索报告 -->
          <el-card shadow="never" class="panel-card">
            <template #header>
              <div class="card-header"><span>智能探索报告</span></div>
            </template>

            <el-form label-position="top">
              <el-form-item label="Webhook 地址">
                <el-input
                  v-model="form.fastbot_webhook"
                  placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxx"
                  clearable
                >
                  <template #prefix><el-icon><Link /></el-icon></template>
                </el-input>
              </el-form-item>

              <el-form-item label="系统访问地址">
                <el-input
                  v-model="form.system_base_url"
                  placeholder="http://localhost:5173"
                  clearable
                  disabled
                >
                  <template #prefix><el-icon><Monitor /></el-icon></template>
                </el-input>
                <div class="form-tip">与左侧共用同一系统地址，用于生成报告链接。</div>
              </el-form-item>

              <div class="form-actions">
                <el-button @click="handleTestFb" :loading="testingFb" :disabled="!form.fastbot_webhook">发送测试消息</el-button>
              </div>
            </el-form>
          </el-card>
        </div>

        <!-- 通知推送使用说明 -->
        <el-card shadow="never" class="tips-card">
          <template #header>
            <div class="card-header"><span>使用说明</span></div>
          </template>
          <div class="tips-content">
            <ol>
              <li>UI 场景报告：填写 Webhook 地址，定时任务执行完毕后自动推送执行结果卡片。</li>
              <li>智能探索报告：填写 Webhook 地址，定时探索任务完成后自动推送探索结果卡片。</li>
              <li>两侧通知可使用相同或不同的 Webhook 地址，实现分群推送。</li>
              <li>点击各板块的测试按钮可单独验证配置是否正确。</li>
            </ol>
          </div>
        </el-card>
      </el-tab-pane>

      <!-- AI 模型配置 Tab -->
      <el-tab-pane label="🤖 AI 模型配置" name="ai">
        <el-card shadow="never" class="ai-card">
          <template #header>
            <div class="card-header">
              <span>AI 模型配置</span>
              <el-tag type="info" size="small" effect="plain">支持 OpenAI / DeepSeek / 通义千问 等</el-tag>
            </div>
          </template>

          <el-form label-position="top" class="ai-form">
            <div class="ai-form-grid">
          <el-form-item label="API Key" class="api-key-item">
            <el-input
              v-model="form.ai_api_key"
              type="password"
              placeholder="sk-xxxxxxxxxxxxxxxxxxxxxxxx"
              clearable
            >
              <template #prefix><el-icon><Key /></el-icon></template>
            </el-input>
            <div class="form-tip">从 AI 模型服务商获取的 API 密钥，请妥善保管。</div>
          </el-form-item>

              <el-form-item label="API 地址 (Base URL)">
                <el-input
                  v-model="form.ai_api_base"
                  placeholder="https://api.openai.com/v1"
                  clearable
                >
                  <template #prefix><el-icon><Link /></el-icon></template>
                </el-input>
                <div class="form-tip">
                  常用地址：
                  <code>https://api.deepseek.com/v1</code> |
                  <code>https://api.openai.com/v1</code> |
                  <code>https://dashscope.aliyuncs.com/compatible-mode/v1</code>
                </div>
              </el-form-item>

              <el-form-item label="模型名称">
                <el-input
                  v-model="form.ai_model"
                  placeholder="gpt-3.5-turbo / deepseek-chat / qwen-turbo"
                  clearable
                >
                  <template #prefix><el-icon><Cpu /></el-icon></template>
                </el-input>
                <div class="form-tip">填写模型标识，如 deepseek-chat、gpt-4o-mini、qwen-turbo 等。</div>
              </el-form-item>
            </div>

            <div class="form-actions">
              <el-button
                @click="handleTestAi"
                :loading="testingAi"
                :disabled="!form.ai_api_key"
              >
                🧪 测试 AI 连接
              </el-button>
            </div>
          </el-form>
        </el-card>

        <!-- AI 配置使用说明 -->
        <el-card shadow="never" class="tips-card">
          <template #header>
            <div class="card-header"><span>使用说明</span></div>
          </template>
          <div class="tips-content">
            <ol>
              <li>AI 智能分析：配置 API Key 和模型后，可在 Fastbot 报告中使用"AI 根因分析"功能。</li>
              <li>支持多种 AI 模型服务商，包括 OpenAI、DeepSeek、通义千问等。</li>
              <li>API Key 会经过加密处理，请妥善保管你的密钥。</li>
              <li>点击"测试 AI 连接"按钮可验证配置是否正确。</li>
            </ol>
          </div>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- 全局保存 -->
    <div class="global-actions">
      <el-button type="primary" size="large" @click="handleSave" :loading="saving">保存全部配置</el-button>
    </div>
  </div>
</template>

<script>
import { Link, Monitor, Key, Cpu } from '@element-plus/icons-vue'
export default {
  components: { Link, Monitor, Key, Cpu }
}
</script>

<style scoped>
.notification-settings {
  padding: 10px;
  padding-bottom: 26px;
  max-width: 1100px;
}

.page-header {
  margin-bottom: 24px;
}

.page-header h2 {
  margin: 0 0 8px;
  font-size: 22px;
  color: #303133;
}

.page-desc {
  margin: 0;
  color: #909399;
  font-size: 14px;
}

.settings-tabs {
  margin-bottom: 20px;
}

.settings-tabs :deep(.el-tabs__header) {
  margin-bottom: 20px;
}

.settings-tabs :deep(.el-tabs__item) {
  font-size: 15px;
  padding: 0 24px;
}

.dual-panel {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}

.panel-card {
  flex: 1;
  min-width: 0;
}

.card-header {
  font-weight: 600;
  font-size: 15px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.form-tip {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
  line-height: 1.5;
}

.form-tip code {
  background: #f0f2f5;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 11px;
  color: #606266;
}

.form-actions {
  margin-top: 12px;
  display: flex;
  gap: 12px;
}

.global-actions {
  margin: 20px 0;
  display: flex;
  justify-content: center;
}

.tips-card {
  margin-bottom: 20px;
}

.tips-content ol {
  margin: 0;
  padding-left: 20px;
  line-height: 2;
  color: #606266;
}

/* AI 配置卡片 */
.ai-card {
  margin-bottom: 0;
}

.ai-card :deep(.el-card__header) {
  background: linear-gradient(90deg, #f5f0ff 0%, #ecf0ff 100%);
}

.ai-form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 20px;
}

.api-key-item {
  grid-column: 1 / -1;
}
</style>
