<script setup>
import { ref, onMounted, watch } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import { useUserStore } from '@/stores/useUserStore'

const userStore = useUserStore()
const loading = ref(false)
const saving = ref(false)
const loadError = ref('')
const testingUi = ref(false)
const testingFb = ref(false)
const testingAi = ref(false)
const activeTab = ref('notification')
const activeFeatureGroups = ref(['core'])
const activeStorageGroups = ref([])
const assetStatus = ref(null)

const form = ref({
  feishu_webhook: '',
  system_base_url: '',
  fastbot_webhook: '',
  ai_api_key: '',
  ai_api_base: '',
  ai_model: '',
  model_inspection: false,
  inspection_identity_v2: false,
  inspection_similarity_convergence: false,
  inspection_exploration_family_convergence: false,
  inspection_coverage_scheduler_v2: false,
  inspection_visual_home_actions: false,
  content_addressed_assets: false,
  tiered_asset_retention: false,
})

const loadAssetStatus = async () => {
  try {
    const response = await api.getAssetStorageStatus()
    assetStatus.value = response.data || null
  } catch {
    assetStatus.value = null
  }
}

const formatBytes = value => {
  if (value === null || value === undefined || value === '') return '-'
  const bytes = Number(value) || 0
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MiB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GiB`
}

const formatPercent = value => (
  value === null || value === undefined || value === '' ? '-' : `${value}%`
)

const loadSettings = async () => {
  loading.value = true
  loadError.value = ''
  try {
    const [res, flagRes] = await Promise.all([
      api.getSettings(),
      api.getFeatureFlags(),
    ])
    const settings = res.data || []
    for (const s of settings) {
      if (s.key === 'feishu_webhook') form.value.feishu_webhook = s.value
      if (s.key === 'system_base_url') form.value.system_base_url = s.value
      if (s.key === 'fastbot_webhook') form.value.fastbot_webhook = s.value
      if (s.key === 'ai_api_key') form.value.ai_api_key = s.value
      if (s.key === 'ai_api_base') form.value.ai_api_base = s.value
      if (s.key === 'ai_model') form.value.ai_model = s.value
    }
    form.value.model_inspection = flagRes.data?.model_inspection === true
    form.value.inspection_identity_v2 = flagRes.data?.inspection_identity_v2 === true
    form.value.inspection_similarity_convergence = flagRes.data?.inspection_similarity_convergence === true
    form.value.inspection_exploration_family_convergence = flagRes.data?.inspection_exploration_family_convergence === true
    form.value.inspection_coverage_scheduler_v2 = flagRes.data?.inspection_coverage_scheduler_v2 === true
    form.value.inspection_visual_home_actions = flagRes.data?.inspection_visual_home_actions === true
    if (!form.value.inspection_identity_v2) {
      form.value.inspection_similarity_convergence = false
      form.value.inspection_exploration_family_convergence = false
      form.value.inspection_coverage_scheduler_v2 = false
      form.value.inspection_visual_home_actions = false
    }
    form.value.content_addressed_assets = flagRes.data?.content_addressed_assets === true
    form.value.tiered_asset_retention = flagRes.data?.tiered_asset_retention === true
    if (!form.value.content_addressed_assets) form.value.tiered_asset_retention = false
  } catch (err) {
    console.error('加载配置失败', err)
    loadError.value = err.response?.data?.detail || err.message || '系统设置加载失败'
    ElMessage.error('系统设置加载失败，请重试后再保存')
  } finally {
    loading.value = false
  }
}

const handleSave = async () => {
  if (loadError.value) {
    ElMessage.warning('配置尚未完整加载，请先重试')
    return
  }
  if (!form.value.content_addressed_assets) form.value.tiered_asset_retention = false
  saving.value = true
  try {
    await api.saveSettings([
      { key: 'feishu_webhook', value: form.value.feishu_webhook, description: 'UI 场景报告 Webhook 地址' },
      { key: 'system_base_url', value: form.value.system_base_url, description: '系统访问基础地址' },
      { key: 'fastbot_webhook', value: form.value.fastbot_webhook, description: '智能探索报告 Webhook 地址' },
      { key: 'ai_api_key', value: form.value.ai_api_key, description: 'AI 模型 API Key' },
      { key: 'ai_api_base', value: form.value.ai_api_base, description: 'AI 模型 API 地址' },
      { key: 'ai_model', value: form.value.ai_model, description: 'AI 模型名称' },
      {
        key: 'model_inspection',
        value: form.value.model_inspection ? 'true' : 'false',
        description: 'Android 模型化智能巡检试验功能',
      },
      { key: 'inspection_identity_v2', value: form.value.inspection_identity_v2 ? 'true' : 'false', description: '巡检 Template/State/Observation 身份模型' },
      { key: 'inspection_similarity_convergence', value: form.value.inspection_similarity_convergence ? 'true' : 'false', description: '巡检高置信相似状态收敛' },
      { key: 'inspection_exploration_family_convergence', value: form.value.inspection_exploration_family_convergence ? 'true' : 'false', description: '巡检同构页面族增量覆盖' },
      { key: 'inspection_coverage_scheduler_v2', value: form.value.inspection_coverage_scheduler_v2 ? 'true' : 'false', description: '巡检覆盖导向优先级调度器' },
      { key: 'inspection_visual_home_actions', value: form.value.inspection_visual_home_actions ? 'true' : 'false', description: 'HOME 无语义图片入口探测' },
      { key: 'content_addressed_assets', value: form.value.content_addressed_assets ? 'true' : 'false', description: '内容寻址报告资产双写' },
      { key: 'tiered_asset_retention', value: form.value.tiered_asset_retention ? 'true' : 'false', description: '报告资产分层保留和清理' },
    ])
    await userStore.fetchFeatureFlags()
    if (activeTab.value === 'features' && activeStorageGroups.value.includes('capacity')) {
      await loadAssetStatus()
    }
    ElMessage.success('配置已保存')
  } catch (err) {
    ElMessage.error('保存失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    saving.value = false
  }
}

watch(() => form.value.inspection_identity_v2, enabled => {
  if (enabled) return
  form.value.inspection_similarity_convergence = false
  form.value.inspection_exploration_family_convergence = false
  form.value.inspection_coverage_scheduler_v2 = false
  form.value.inspection_visual_home_actions = false
})

watch(() => form.value.model_inspection, enabled => {
  if (enabled) return
  form.value.inspection_identity_v2 = false
})

watch(() => form.value.inspection_coverage_scheduler_v2, enabled => {
  if (enabled) return
  form.value.inspection_visual_home_actions = false
})

watch(() => form.value.content_addressed_assets, enabled => {
  if (enabled) return
  form.value.tiered_asset_retention = false
})

watch([activeTab, activeStorageGroups], ([tab, groups]) => {
  if (tab === 'features' && groups.includes('capacity') && !assetStatus.value) {
    loadAssetStatus()
  }
}, { deep: true })

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
    <div class="settings-shell">
      <div class="page-header">
        <h2>系统设置</h2>
        <p class="page-desc">管理通知、AI 服务和试验功能。</p>
      </div>
      <el-alert
        v-if="loadError"
        title="系统设置未完整加载，已禁止保存以免覆盖现有配置"
        type="error"
        :closable="false"
        show-icon
      >
        <template #default><el-button link type="primary" @click="loadSettings">重新加载</el-button></template>
      </el-alert>

    <!-- Tab 切换 -->
      <el-tabs v-model="activeTab" class="settings-tabs">
      <!-- 通知推送 Tab -->
      <el-tab-pane label="通知推送" name="notification">
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
      <el-tab-pane label="AI 模型" name="ai">
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
              <li>API Key 属于敏感配置，请仅在受信任的管理环境中填写。</li>
              <li>点击"测试 AI 连接"按钮可验证配置是否正确。</li>
            </ol>
          </div>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="试验功能" name="features">
        <el-card shadow="never" class="feature-card">
          <template #header>
            <div class="card-header">
              <span>智能巡检试验功能</span>
              <el-tag type="warning" size="small" effect="plain">试验功能</el-tag>
            </div>
          </template>
          <el-collapse v-model="activeFeatureGroups" class="feature-groups">
            <el-collapse-item name="core">
              <template #title>
                <div class="feature-group-title">
                  <strong>巡检核心</strong>
                  <span>功能入口与页面身份识别</span>
                </div>
              </template>
              <div class="feature-setting-row">
                <div>
                  <div class="feature-name">启用智能巡检</div>
                  <div class="feature-desc">显示专项测试、巡检报告、兼容回放和定时巡检入口。</div>
                </div>
                <el-switch v-model="form.model_inspection" />
              </div>
              <div class="feature-setting-row">
                <div>
                  <div class="feature-name">页面身份识别</div>
                  <div class="feature-desc">区分页面实例、采集记录与同构页面族。</div>
                </div>
                <div class="feature-control">
                  <el-switch v-model="form.inspection_identity_v2" :disabled="!form.model_inspection" />
                  <span v-if="!form.model_inspection" class="dependency-reason">需先启用智能巡检</span>
                </div>
              </div>
            </el-collapse-item>

            <el-collapse-item name="coverage">
              <template #title>
                <div class="feature-group-title">
                  <strong>覆盖效率</strong>
                  <span>优先探索新页面并减少重复操作</span>
                </div>
              </template>
              <div class="feature-setting-row">
                <div>
                  <div class="feature-name">同类页面复用</div>
                  <div class="feature-desc">保留每个业务页面，仅跳过同类页面已覆盖的重复动作。</div>
                </div>
                <div class="feature-control">
                  <el-switch v-model="form.inspection_exploration_family_convergence" :disabled="!form.inspection_identity_v2" />
                  <span v-if="!form.inspection_identity_v2" class="dependency-reason">需先启用页面身份识别</span>
                </div>
              </div>
              <div class="feature-setting-row">
                <div>
                  <div class="feature-name">覆盖优先调度</div>
                  <div class="feature-desc">优先展开新页面族和关键业务路径。</div>
                </div>
                <div class="feature-control">
                  <el-switch v-model="form.inspection_coverage_scheduler_v2" :disabled="!form.inspection_identity_v2" />
                  <span v-if="!form.inspection_identity_v2" class="dependency-reason">需先启用页面身份识别</span>
                </div>
              </div>
              <div class="feature-setting-row">
                <div>
                  <div class="feature-name">首页图片入口</div>
                  <div class="feature-desc">识别没有文字说明但可以跳转的首页图片。</div>
                </div>
                <div class="feature-control">
                  <el-switch v-model="form.inspection_visual_home_actions" :disabled="!form.inspection_coverage_scheduler_v2" />
                  <span v-if="!form.inspection_coverage_scheduler_v2" class="dependency-reason">需先启用覆盖优先调度</span>
                </div>
              </div>
            </el-collapse-item>

            <el-collapse-item name="advanced">
              <template #title>
                <div class="feature-group-title">
                  <strong>高级试验</strong>
                  <span>需要观察误判情况后再逐步开启</span>
                </div>
              </template>
              <div class="feature-setting-row">
                <div>
                  <div class="feature-name">相似页面收敛</div>
                  <div class="feature-desc">仅在高置信度下复用相似页面的探索结果。</div>
                </div>
                <div class="feature-control">
                  <el-switch v-model="form.inspection_similarity_convergence" :disabled="!form.inspection_identity_v2" />
                  <span v-if="!form.inspection_identity_v2" class="dependency-reason">需先启用页面身份识别</span>
                </div>
              </div>
            </el-collapse-item>

            <el-collapse-item name="assets">
              <template #title>
                <div class="feature-group-title">
                  <strong>资产治理</strong>
                  <span>降低截图、XML 和报告的磁盘占用</span>
                </div>
              </template>
              <div class="feature-setting-row">
                <div>
                  <div class="feature-name">报告资产去重</div>
                  <div class="feature-desc">相同内容只保存一份，由巡检和兼容任务共同引用。</div>
                </div>
                <el-switch v-model="form.content_addressed_assets" />
              </div>
              <div class="feature-setting-row">
                <div>
                  <div class="feature-name">分层保留</div>
                  <div class="feature-desc">按 7 天、90 天和长期引用分层保留报告资产。</div>
                </div>
                <div class="feature-control">
                  <el-switch v-model="form.tiered_asset_retention" :disabled="!form.content_addressed_assets" />
                  <span v-if="!form.content_addressed_assets" class="dependency-reason">需先启用报告资产去重</span>
                </div>
              </div>
            </el-collapse-item>
          </el-collapse>
        </el-card>

        <el-collapse v-model="activeStorageGroups" class="storage-collapse">
          <el-collapse-item name="capacity">
            <template #title>
              <div class="storage-collapse-title">
                <div>
                  <strong>报告资产容量</strong>
                  <span>磁盘 {{ formatPercent(assetStatus?.used_percent ?? assetStatus?.disk_used_percent) }} · 可回收 {{ formatBytes(assetStatus?.reclaimable_bytes) }}</span>
                </div>
                <el-button link type="primary" @click.stop="loadAssetStatus">刷新</el-button>
              </div>
            </template>
            <div class="storage-stats">
              <div><span>单任务上限</span><strong>512 MiB</strong></div>
              <div><span>200 页面目标</span><strong>200 MiB</strong></div>
              <div><span>磁盘水位</span><strong>{{ formatPercent(assetStatus?.used_percent ?? assetStatus?.disk_used_percent) }}</strong></div>
              <div><span>去重资产</span><strong>{{ formatBytes(assetStatus?.stored_bytes ?? assetStatus?.cas_bytes ?? assetStatus?.store_bytes) }}</strong></div>
              <div><span>长期保留引用</span><strong>{{ assetStatus?.pinned_reference_count ?? assetStatus?.pinned_count ?? 0 }}</strong></div>
              <div><span>可回收</span><strong>{{ formatBytes(assetStatus?.reclaimable_bytes) }}</strong></div>
              <div><span>完整资产保留</span><strong>7 天</strong></div>
              <div><span>代表资产保留</span><strong>90 天</strong></div>
              <div><span>旧文件回滚</span><strong>14 天</strong></div>
            </div>
            <el-alert
              v-if="assetStatus && assetStatus.can_start === false"
              title="磁盘水位达到临界值，新的巡检和兼容性任务已暂停"
              type="error"
              :closable="false"
              show-icon
            />
            <el-alert
              v-else-if="assetStatus && ['WATCH', 'HIGH'].includes(assetStatus.pressure_level)"
              :title="assetStatus.pressure_level === 'HIGH' ? '磁盘水位超过 90%，正在优先回收已过期资产' : '磁盘水位超过 80%，请关注报告资产增长'"
              type="warning"
              :closable="false"
              show-icon
            />
          </el-collapse-item>
        </el-collapse>
      </el-tab-pane>
      </el-tabs>

      <div class="global-actions">
        <el-button type="primary" :disabled="Boolean(loadError)" @click="handleSave" :loading="saving">保存全部配置</el-button>
      </div>
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
  flex: 1;
  width: 100%;
  height: 0;
  min-height: 0;
  box-sizing: border-box;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
  background: #f2f3f5;
}

.settings-shell {
  width: 100%;
  min-height: 100%;
  max-width: 1100px;
  min-width: 0;
  margin: 0 auto;
  padding: 16px 16px 0;
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
  container-name: settings-shell;
  container-type: inline-size;
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
  flex: 1;
  min-height: 0;
  margin-bottom: 0;
}

.settings-tabs :deep(.el-tabs__content),
.settings-tabs :deep(.el-tab-pane) {
  overflow: visible;
}

.settings-tabs :deep(.el-tabs__header) {
  margin-bottom: 20px;
}

.settings-tabs :deep(.el-tabs__item) {
  font-size: 15px;
  padding: 0 24px;
}

.dual-panel {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(360px, 100%), 1fr));
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
  position: sticky;
  z-index: 10;
  bottom: 0;
  margin-top: 16px;
  padding: 12px 0 16px;
  display: flex;
  justify-content: flex-end;
  border-top: 1px solid #dcdfe6;
  background: #f2f3f5;
}

.storage-collapse { margin-top: 12px; padding: 0 16px; border: 1px solid #dcdfe6; background: #fff; }
.storage-collapse :deep(.el-collapse-item__header) { min-height: 58px; height: auto; line-height: 1.35; }
.storage-collapse-title { min-width: 0; width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-right: 12px; }
.storage-collapse-title > div { min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.storage-collapse-title strong { color: #303133; font-size: 14px; }
.storage-collapse-title span { overflow: hidden; color: #909399; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.storage-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1px; margin-bottom: 14px; background: #ebeef5; border: 1px solid #ebeef5; }
.storage-stats > div { min-height: 66px; padding: 10px 12px; display: flex; flex-direction: column; justify-content: center; gap: 6px; background: #fff; }
.storage-stats span { color: #909399; font-size: 12px; }
.storage-stats strong { color: #303133; font-size: 16px; }

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
.ai-card, .feature-card {
  margin-bottom: 0;
}

.ai-card :deep(.el-card__header) {
  background: #f7f8fa;
}

.feature-card :deep(.el-card__body) { padding: 0 20px; }
.feature-groups { border: 0; }
.feature-groups :deep(.el-collapse-item__header) { min-height: 52px; height: auto; line-height: 1.3; }
.feature-groups :deep(.el-collapse-item__content) { padding-bottom: 8px; }
.feature-group-title { min-width: 0; display: flex; align-items: baseline; gap: 12px; }
.feature-group-title strong { color: #303133; font-size: 14px; }
.feature-group-title span { color: #909399; font-size: 12px; font-weight: 400; }
.feature-setting-row { min-height: 56px; display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 9px 0; border-top: 1px solid #f0f2f5; }
.feature-setting-row > div { min-width: 0; }
.feature-setting-row :deep(.el-switch) { flex-shrink: 0; }
.feature-name { color: #303133; font-size: 13px; font-weight: 600; }
.feature-desc { margin-top: 3px; color: #909399; font-size: 12px; line-height: 1.45; }
.feature-control { max-width: 180px; flex-shrink: 0; display: flex; align-items: flex-end; flex-direction: column; gap: 3px; }
.dependency-reason { color: #a56a00; font-size: 11px; line-height: 1.35; text-align: right; }

.ai-form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 20px;
}

.api-key-item {
  grid-column: 1 / -1;
}

@media (max-width: 800px) {
  .settings-shell { padding: 10px 10px 0; }
  .page-header { margin-bottom: 14px; }
  .settings-tabs :deep(.el-tabs__item) { padding: 0 14px; }
  .dual-panel { flex-direction: column; }
  .panel-card { width: 100%; }
  .storage-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ai-form-grid { grid-template-columns: 1fr; }
  .feature-group-title { align-items: flex-start; flex-direction: column; gap: 2px; padding: 8px 0; }
  .feature-setting-row { gap: 12px; }
}

@container settings-shell (max-width: 760px) {
  .page-header { margin-bottom: 14px; }
  .settings-tabs :deep(.el-tabs__item) { padding: 0 14px; }
  .storage-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ai-form-grid { grid-template-columns: 1fr; }
  .feature-group-title { align-items: flex-start; flex-direction: column; gap: 2px; padding: 8px 0; }
  .feature-setting-row { align-items: flex-start; gap: 12px; }
  .feature-control { max-width: 150px; }
}

@media (max-height: 620px) {
  .settings-shell { padding-top: 8px; }
  .page-header { margin-bottom: 8px; }
  .page-header h2 { margin-bottom: 2px; font-size: 18px; }
  .settings-tabs :deep(.el-tabs__header) { margin-bottom: 10px; }
  .global-actions { margin-top: 8px; padding-top: 8px; padding-bottom: 8px; }
}
</style>
