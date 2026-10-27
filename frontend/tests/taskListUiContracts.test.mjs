import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { computed, ref, reactive, nextTick } from 'vue'
import dayjs from 'dayjs'

import { scheduledTaskExecution } from '../src/utils/scheduledTaskPresentation.js'

// 定时任务列表契约测试：
// - 列表不再展示运行环境信息（执行内容里不出现「环境：xxx」）
// - 编辑表单的运行环境选择与 env_id 保存逻辑保持不变
// - 操作列「编辑」与「更多」三点共用 flex 交叉轴中线，垂直居中对齐
// 与仓库既有测试一致，用 node:test + stripImports 依赖注入执行 <script setup>（非 vitest）。

const stripImports = source => source.replace(/^import [\s\S]*? from ['"][^'"]+['"]\s*\n/gm, '')
const taskListSource = readFileSync(new URL('../src/views/tasks/TaskList.vue', import.meta.url), 'utf8')
const taskListScript = stripImports(taskListSource.match(/<script setup>([\s\S]*?)<\/script>/)[1])
const template = taskListSource.slice(taskListSource.indexOf('<template>'), taskListSource.lastIndexOf('</template>'))
const styles = taskListSource.slice(taskListSource.indexOf('<style scoped>'))

function taskListSetup({ items = [] } = {}) {
  const mounted = []
  const apiCalls = []
  const route = { fullPath: '/execution/tasks', path: '/execution/tasks', query: {} }
  const api = {
    getTasks: async (...args) => { apiCalls.push(['getTasks', ...args]); return { data: { items, total: items.length } } },
    getEnvironments: async () => ({ data: [{ id: 2, name: '预发布' }] }),
    getDeviceList: async () => ({ data: [] }),
    getPackages: async () => ({ data: { items: [], total: 0 } }),
    getInspectionProfiles: async () => ({ data: [] }),
    getScenarios: async () => ({ data: { items: [] } }),
    getScenario: async () => ({ data: null }),
    apiTesting: { get: async () => ({ data: { items: [] } }) },
    createTask: async () => ({ data: {} }),
    updateTask: async () => ({ data: {} }),
    toggleTask: async () => ({ data: {} }),
    deleteTask: async () => ({ data: {} }),
  }
  const deps = {
    computed, ref, reactive, nextTick,
    onMounted: fn => mounted.push(fn),
    watch: (getter, cb, options) => { if (options?.immediate) cb(getter(), undefined) },
    useRoute: () => route,
    ElMessage: { success: () => {}, error: () => {}, warning: () => {}, info: () => {} },
    ElMessageBox: { confirm: async () => 'confirm' },
    Plus: 'Plus', Delete: 'Delete', MoreFilled: 'MoreFilled', Search: 'Search', Refresh: 'Refresh', Edit: 'Edit',
    api,
    dayjs,
    useRemoteSearch: () => ({ options: ref([]), loading: ref(false), search: () => {}, ensureOption: () => {} }),
    useUserStore: () => ({ featureFlags: { model_inspection: false } }),
    scheduledTaskExecution,
  }
  const exports = 'tasks,taskRows,environments,form,buildPayload,fetchTasks,fetchEnvironments'
  const state = new Function(...Object.keys(deps), taskListScript + `\nreturn {${exports}}`)(...Object.values(deps))
  return { state, mounted, apiCalls }
}

const uiTask = {
  id: 1, name: '每日回归', scenario_id: 5, scenario_name: '登录回归',
  device_serials: ['serial-1'], strategy: 'DAILY', enable_notification: true,
  strategy_config: { _task_type: 'ui', hour: 8, minute: 0, env_id: 2 },
}
const apiTask = {
  id: 2, name: '接口巡检', scenario_id: null, scenario_name: '订单回归',
  device_serials: [], strategy: 'DAILY', enable_notification: true,
  strategy_config: { _task_type: 'api', hour: 8, minute: 0, env_id: 2, api_scenario_id: 9 },
}

test('列表不再展示运行环境信息（配置了环境也不出现「环境：xxx」）', async () => {
  const { state, mounted } = taskListSetup({ items: [uiTask, apiTask] })
  await Promise.all(mounted.map(fn => fn()))
  await state.fetchEnvironments()
  await state.fetchTasks()

  assert.equal(state.environments.value.length, 1, '运行环境仍被拉取，供编辑表单使用')
  assert.equal(state.taskRows.value.length, 2)

  const [uiRow, apiRow] = state.taskRows.value
  assert.doesNotMatch(`${uiRow._execution.title} ${uiRow._execution.detail}`, /环境|预发布/)
  assert.equal(uiRow._execution.detail, '')
  assert.doesNotMatch(`${apiRow._execution.title} ${apiRow._execution.detail}`, /环境|预发布/)
  assert.match(apiRow._execution.detail, /无需设备/, '设备信息保留，只去掉环境信息')
})

test('编辑表单的运行环境选择与 env_id 保存逻辑保持不变', async () => {
  const { state } = taskListSetup()
  assert.equal(state.form.env_id, null)

  state.form.task_type = 'ui'
  state.form.scenario_id = 5
  state.form.env_id = 2
  state.form.strategy = 'DAILY'
  state.form.daily_time = '08:30'
  assert.equal(state.buildPayload().strategy_config.env_id, 2, 'UI 任务仍保存 env_id')

  state.form.task_type = 'api'
  state.form.api_scenario_id = 9
  state.form.env_id = 2
  assert.equal(state.buildPayload().strategy_config.env_id, 2, '接口任务仍保存 env_id')

  state.form.task_type = 'ui'
  state.form.env_id = null
  assert.equal(state.buildPayload().strategy_config.env_id, undefined, '未选环境时保持原样不写入')
})

test('任务表格不再有运行环境列，编辑表单仍保留运行环境选择', () => {
  const tableStart = template.indexOf('<el-table')
  const tableEnd = template.indexOf('</el-table>', tableStart)
  const table = template.slice(tableStart, tableEnd)
  assert.ok(tableStart >= 0 && tableEnd > tableStart, '任务表格必须存在')
  assert.doesNotMatch(table, /环境/, '表格列里不允许出现运行环境信息')

  const dialogStart = template.indexOf('task-editor-dialog')
  const dialog = template.slice(dialogStart)
  assert.match(dialog, /<el-form-item v-if="\['ui', 'api'\]\.includes\(form\.task_type\)" label="运行环境">/)
  assert.match(dialog, /v-model="form\.env_id"/)
  assert.match(dialog, /v-for="env in environments"/)
})

test('操作列「编辑」与「更多」三点在同一 flex 中线垂直居中', () => {
  const actionsStart = template.indexOf('class="task-actions"')
  assert.ok(actionsStart >= 0, '操作列需要统一的对齐容器')
  const actions = template.slice(actionsStart, template.indexOf('</div>', actionsStart))
  assert.match(actions, /<el-button link type="primary" @click="handleEdit\(row\)">编辑<\/el-button>/)
  assert.match(actions, /<el-dropdown trigger="click" @command="handleDelete\(row\)">/)
  assert.match(actions, /:icon="MoreFilled"/)

  assert.match(styles, /\.task-actions\s*\{[^}]*display:\s*flex;[^}]*align-items:\s*center;/)
  assert.match(styles, /\.task-actions :deep\(\.el-dropdown\)\s*\{[^}]*align-items:\s*center;/)
})
