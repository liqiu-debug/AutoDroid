<script setup>
import { ref, reactive, onMounted, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Delete, Search, Refresh, Edit, Timer, Calendar, Clock } from '@element-plus/icons-vue'
import api from '@/api'
import dayjs from 'dayjs'

// ---- 数据 ----
const tasks = ref([])
const scenarios = ref([])
const devices = ref([])
const environments = ref([])
const loading = ref(false)
const searchQuery = ref('')

// 弹窗
const dialogVisible = ref(false)
const dialogTitle = ref('新建定时任务')
const editingId = ref(null)

const form = reactive({
    name: '',
    task_type: 'ui',  // ui | fastbot
    scenario_id: null,
    device_serials: [],
    env_id: null,
    strategy: 'DAILY',
    // DAILY
    daily_time: '08:00',
    // WEEKLY
    weekly_days: [],
    weekly_time: '08:00',
    // INTERVAL
    interval_value: 30,
    interval_unit: 'minutes',
    // ONCE
    once_datetime: '',
    // 通知
    enable_notification: true,
    // Fastbot 专属
    fb_package_name: 'com.ehaier.zgq.shop.mall',
    fb_duration_min: 30,
    fb_throttle: 500,
    fb_ignore_crashes: false,
})

// ---- 计算属性 ----
const filteredTasks = computed(() => {
    if (!searchQuery.value) return tasks.value
    const q = searchQuery.value.toLowerCase()
    return tasks.value.filter(t => t.name.toLowerCase().includes(q) || (t.scenario_name || '').toLowerCase().includes(q))
})

// ---- 方法 ----
const fetchTasks = async () => {
    loading.value = true
    try {
        const res = await api.getTasks()
        tasks.value = res.data || []
    } catch (err) {
        ElMessage.error('获取任务列表失败')
    } finally {
        loading.value = false
    }
}

const fetchScenarios = async () => {
    try {
        const res = await api.getScenarios()
        scenarios.value = res.data.items || res.data || []
    } catch (err) {
        console.error('获取场景列表失败', err)
    }
}

const fetchDevices = async () => {
    try {
        const res = await api.getDeviceList()
        devices.value = (res.data || []).filter(d => d.status !== 'OFFLINE')
    } catch (err) {
        console.error('获取设备列表失败', err)
    }
}

const fetchEnvironments = async () => {
    try {
        const res = await api.getEnvironments()
        environments.value = res.data || []
    } catch (err) {
        console.error('获取环境列表失败', err)
    }
}

const resetForm = () => {
    form.name = ''
    form.task_type = 'ui'
    form.scenario_id = null
    form.device_serials = []
    form.env_id = null
    form.strategy = 'DAILY'
    form.daily_time = '08:00'
    form.weekly_days = []
    form.weekly_time = '08:00'
    form.interval_value = 30
    form.interval_unit = 'minutes'
    form.once_datetime = ''
    form.enable_notification = true
    form.fb_package_name = 'com.ehaier.zgq.shop.mall'
    form.fb_duration_min = 30
    form.fb_throttle = 500
    form.fb_ignore_crashes = false
    editingId.value = null
}

const handleCreate = () => {
    resetForm()
    dialogTitle.value = '新建定时任务'
    dialogVisible.value = true
}

const handleEdit = (row) => {
    editingId.value = row.id
    dialogTitle.value = '编辑定时任务'
    form.name = row.name
    form.scenario_id = row.scenario_id
    form.device_serials = row.device_serials || []
    form.strategy = row.strategy

    const config = row.strategy_config || {}
    form.env_id = config.env_id || null

    // 判断任务类型
    if (config._task_type === 'fastbot') {
        form.task_type = 'fastbot'
        form.fb_package_name = config.fb_package_name || ''
        form.fb_duration_min = Math.round((config.fb_duration || 1800) / 60)
        form.fb_throttle = config.fb_throttle || 500
        form.fb_ignore_crashes = config.fb_ignore_crashes || false
    } else {
        form.task_type = 'ui'
    }

    if (row.strategy === 'DAILY') {
        form.daily_time = `${String(config.hour || 0).padStart(2, '0')}:${String(config.minute || 0).padStart(2, '0')}`
    } else if (row.strategy === 'WEEKLY') {
        form.weekly_days = config.days || []
        form.weekly_time = `${String(config.hour || 0).padStart(2, '0')}:${String(config.minute || 0).padStart(2, '0')}`
    } else if (row.strategy === 'INTERVAL') {
        form.interval_value = config.interval_value || 30
        form.interval_unit = config.interval_unit || 'minutes'
    } else if (row.strategy === 'ONCE') {
        form.once_datetime = config.run_date || ''
    }
    form.enable_notification = row.enable_notification !== false
    dialogVisible.value = true
}

const buildPayload = () => {
    const payload = {
        name: form.name,
        scenario_id: form.scenario_id,
        device_serials: form.device_serials,
        strategy: form.strategy,
        strategy_config: {},
        enable_notification: form.enable_notification,
    }

    // 调度时间配置
    if (form.strategy === 'DAILY') {
        const [h, m] = form.daily_time.split(':').map(Number)
        payload.strategy_config = { hour: h, minute: m }
    } else if (form.strategy === 'WEEKLY') {
        const [h, m] = form.weekly_time.split(':').map(Number)
        payload.strategy_config = { days: form.weekly_days, hour: h, minute: m }
    } else if (form.strategy === 'INTERVAL') {
        payload.strategy_config = { interval_value: form.interval_value, interval_unit: form.interval_unit }
    } else if (form.strategy === 'ONCE') {
        payload.strategy_config = { run_date: form.once_datetime }
    }

    // 附加任务类型标记和 Fastbot/UI 配置
    payload.strategy_config._task_type = form.task_type
    if (form.task_type === 'fastbot') {
        payload.strategy_config.fb_package_name = form.fb_package_name
        payload.strategy_config.fb_duration = form.fb_duration_min * 60
        payload.strategy_config.fb_throttle = form.fb_throttle
        payload.strategy_config.fb_ignore_crashes = form.fb_ignore_crashes
    } else if (form.task_type === 'ui') {
        if (form.env_id) {
            payload.strategy_config.env_id = form.env_id
        }
    }

    return payload
}

const handleSubmit = async () => {
    if (!form.name) return ElMessage.warning('请输入任务名称')
    if (form.task_type === 'ui' && !form.scenario_id) return ElMessage.warning('请选择执行场景')
    if (form.task_type === 'fastbot' && !form.fb_package_name) return ElMessage.warning('请输入目标包名')
    if (form.task_type === 'fastbot' && (!form.device_serials || form.device_serials.length === 0)) return ElMessage.warning('智能探索任务必须选择执行设备')
    if (form.strategy === 'WEEKLY' && form.weekly_days.length === 0) {
        return ElMessage.warning('请至少选择一天')
    }
    if (form.strategy === 'ONCE' && !form.once_datetime) {
        return ElMessage.warning('请选择执行日期时间')
    }

    const payload = buildPayload()
    try {
        if (editingId.value) {
            await api.updateTask(editingId.value, payload)
            ElMessage.success('任务已更新')
        } else {
            await api.createTask(payload)
            ElMessage.success('任务已创建')
        }
        dialogVisible.value = false
        fetchTasks()
    } catch (err) {
        ElMessage.error('操作失败: ' + (err.response?.data?.detail || err.message))
    }
}

const handleToggle = async (row) => {
    try {
        await api.toggleTask(row.id)
        fetchTasks()
    } catch (err) {
        ElMessage.error('切换失败')
    }
}

const handleDelete = async (row) => {
    try {
        await ElMessageBox.confirm(`确定删除定时任务 "${row.name}"?`, '警告', {
            type: 'warning',
            confirmButtonText: '删除',
            cancelButtonText: '取消',
        })
        await api.deleteTask(row.id)
        ElMessage.success('已删除')
        fetchTasks()
    } catch (err) {
        if (err !== 'cancel') ElMessage.error('删除失败')
    }
}

const formatTime = (t) => {
    if (!t) return '-'
    return dayjs(t).format('MM-DD HH:mm')
}

const getTaskTypeLabel = (row) => {
    try {
        const config = row.strategy_config || {}
        return config._task_type === 'fastbot' ? '智能探索' : 'UI自动化'
    } catch { return 'UI自动化' }
}

const getEnvName = (row) => {
    try {
        const envId = row.strategy_config?.env_id
        if (!envId) return '-'
        const env = environments.value.find(e => e.id === envId)
        return env ? env.name : `[环境已删除 ID:${envId}]`
    } catch { return '-' }
}

const weekDayOptions = [
    { label: '一', value: 0 },
    { label: '二', value: 1 },
    { label: '三', value: 2 },
    { label: '四', value: 3 },
    { label: '五', value: 4 },
    { label: '六', value: 5 },
    { label: '日', value: 6 },
]

onMounted(() => {
    fetchTasks()
    fetchScenarios()
    fetchDevices()
    fetchEnvironments()
})
</script>

<template>
    <div class="task-list-container">
        <div class="content-wrapper">
            <div class="toolbar">
                <div class="left-tools">
                    <el-input
                        v-model="searchQuery"
                        placeholder="搜索任务名称..."
                        class="search-input"
                        :prefix-icon="Search"
                        clearable
                    />
                    <el-button :icon="Refresh" @click="fetchTasks" circle />
                </div>
                <div class="right-tools">
                    <el-button type="primary" :icon="Plus" @click="handleCreate">新建任务</el-button>
                </div>
            </div>

            <el-table
                :data="filteredTasks"
                v-loading="loading"
                style="width: 100%"
                height="calc(100vh - 160px)"
                :header-cell-style="{ background: '#f5f7fa', color: '#606266' }"
            >
                <el-table-column prop="id" label="ID" width="60" align="center" />

                <el-table-column label="任务名称" min-width="160">
                    <template #default="{ row }">
                        <span class="task-name" @click="handleEdit(row)">{{ row.name }}</span>
                    </template>
                </el-table-column>

                <el-table-column label="任务类型" width="110" align="center">
                    <template #default="{ row }">
                        <el-tag size="small" :type="getTaskTypeLabel(row) === '智能探索' ? 'warning' : ''" effect="plain">
                            {{ getTaskTypeLabel(row) }}
                        </el-tag>
                    </template>
                </el-table-column>

                <el-table-column label="执行目标" min-width="140">
                    <template #default="{ row }">
                        <span>{{ row.scenario_name }}</span>
                    </template>
                </el-table-column>

                <el-table-column label="运行环境" min-width="100">
                    <template #default="{ row }">
                        <span class="text-gray">{{ getEnvName(row) }}</span>
                    </template>
                </el-table-column>

                <el-table-column label="触发规则" min-width="160">
                    <template #default="{ row }">
                        <span class="schedule-text">{{ row.formatted_schedule }}</span>
                    </template>
                </el-table-column>

                <el-table-column label="下次运行" width="140" align="center">
                    <template #default="{ row }">
                        <span :class="{ 'text-gray': !row.next_run_time }">{{ formatTime(row.next_run_time) }}</span>
                    </template>
                </el-table-column>

                <el-table-column label="状态" width="80" align="center">
                    <template #default="{ row }">
                        <el-switch
                            :model-value="row.is_active"
                            @change="handleToggle(row)"
                            size="small"
                        />
                    </template>
                </el-table-column>

                <el-table-column label="操作" width="120" align="center" fixed="right">
                    <template #default="{ row }">
                        <el-tooltip content="编辑" placement="top">
                            <el-button :icon="Edit" link type="primary" @click="handleEdit(row)" />
                        </el-tooltip>
                        <el-tooltip content="删除" placement="top">
                            <el-button :icon="Delete" link type="danger" @click="handleDelete(row)" />
                        </el-tooltip>
                    </template>
                </el-table-column>
            </el-table>
        </div>

        <!-- 新建/编辑弹窗 -->
        <el-dialog v-model="dialogVisible" :title="dialogTitle" width="560px" destroy-on-close>
            <el-form label-width="90px" class="task-form">
                <el-form-item label="任务名称">
                    <el-input v-model="form.name" placeholder="例如：每日回归测试" />
                </el-form-item>

                <el-form-item label="任务类型">
                    <el-radio-group v-model="form.task_type">
                        <el-radio-button value="ui">UI 自动化</el-radio-button>
                        <el-radio-button value="fastbot">智能探索</el-radio-button>
                    </el-radio-group>
                </el-form-item>

                <!-- UI 自动化专属：场景选择 -->
                <el-form-item v-if="form.task_type === 'ui'" label="执行场景">
                    <el-select v-model="form.scenario_id" placeholder="选择场景" style="width: 100%">
                        <el-option
                            v-for="s in scenarios"
                            :key="s.id"
                            :label="s.name"
                            :value="s.id"
                        />
                    </el-select>
                </el-form-item>

                <!-- 智能探索专属：Fastbot 配置 -->
                <template v-if="form.task_type === 'fastbot'">
                    <el-form-item label="目标包名">
                        <el-input v-model="form.fb_package_name" placeholder="com.example.app" />
                    </el-form-item>
                    <el-form-item label="探索时长">
                        <div style="display: flex; align-items: center; gap: 8px">
                            <el-input-number v-model="form.fb_duration_min" :min="1" :max="120" :step="5" style="width: 160px" />
                            <span class="unit-label">分钟</span>
                        </div>
                    </el-form-item>
                    <el-form-item label="操作频率">
                        <div style="display: flex; align-items: center; gap: 8px">
                            <el-input-number v-model="form.fb_throttle" :min="0" :max="1000" :step="50" style="width: 160px" />
                            <span class="unit-label">ms</span>
                        </div>
                    </el-form-item>
                    <el-form-item label="忽略崩溃">
                        <el-switch v-model="form.fb_ignore_crashes" active-text="继续探索" inactive-text="立即停止" />
                    </el-form-item>
                </template>

                <el-form-item label="执行设备">
                    <el-select
                        v-model="form.device_serials"
                        :placeholder="form.task_type === 'fastbot' ? '请选择运行设备' : '选择设备 (可选)'"
                        multiple
                        collapse-tags
                        clearable
                        style="width: 100%"
                        @focus="fetchDevices"
                    >
                        <el-option
                            v-for="d in devices"
                            :key="d.serial"
                            :label="d.custom_name || d.market_name || d.model || d.serial"
                            :value="d.serial"
                        />
                    </el-select>
                </el-form-item>

                <el-form-item v-if="form.task_type === 'ui'" label="运行环境">
                    <el-select
                        v-model="form.env_id"
                        placeholder="选择环境 (可选)"
                        clearable
                        style="width: 100%"
                    >
                        <el-option
                            v-for="env in environments"
                            :key="env.id"
                            :label="env.name"
                            :value="env.id"
                        />
                    </el-select>
                </el-form-item>

                <el-form-item label="执行策略">
                    <el-select v-model="form.strategy" style="width: 100%">
                        <el-option label="单次" value="ONCE" />
                        <el-option label="每天" value="DAILY" />
                        <el-option label="每周" value="WEEKLY" />
                        <el-option label="循环" value="INTERVAL" />
                    </el-select>
                </el-form-item>

                <!-- DAILY -->
                <el-form-item v-if="form.strategy === 'DAILY'" label="执行时间">
                    <el-time-select
                        v-model="form.daily_time"
                        start="00:00"
                        step="00:30"
                        end="23:30"
                        placeholder="选择时间"
                        style="width: 100%"
                    />
                </el-form-item>

                <!-- WEEKLY -->
                <template v-if="form.strategy === 'WEEKLY'">
                    <el-form-item label="选择星期">
                        <el-checkbox-group v-model="form.weekly_days" class="week-checkbox">
                            <el-checkbox-button
                                v-for="day in weekDayOptions"
                                :key="day.value"
                                :value="day.value"
                            >
                                {{ day.label }}
                            </el-checkbox-button>
                        </el-checkbox-group>
                    </el-form-item>
                    <el-form-item label="执行时间">
                        <el-time-select
                            v-model="form.weekly_time"
                            start="00:00"
                            step="00:30"
                            end="23:30"
                            placeholder="选择时间"
                            style="width: 100%"
                        />
                    </el-form-item>
                </template>

                <!-- INTERVAL -->
                <el-form-item v-if="form.strategy === 'INTERVAL'" label="执行间隔">
                    <div style="display: flex; gap: 10px; width: 100%">
                        <el-input-number v-model="form.interval_value" :min="1" :max="1440" style="flex: 1" />
                        <el-select v-model="form.interval_unit" style="width: 100px">
                            <el-option label="分钟" value="minutes" />
                            <el-option label="小时" value="hours" />
                        </el-select>
                    </div>
                </el-form-item>

                <!-- ONCE -->
                <el-form-item v-if="form.strategy === 'ONCE'" label="执行时间">
                    <el-date-picker
                        v-model="form.once_datetime"
                        type="datetime"
                        placeholder="选择日期和时间"
                        format="YYYY-MM-DD HH:mm:ss"
                        value-format="YYYY-MM-DDTHH:mm:ss"
                        :disabled-date="(date) => date < new Date(new Date().setHours(0,0,0,0))"
                        style="width: 100%"
                    />
                </el-form-item>

                <el-form-item label="飞书通知">
                    <el-switch
                        v-model="form.enable_notification"
                        active-text="执行后发送通知"
                        inactive-text=""
                    />
                </el-form-item>
            </el-form>

            <template #footer>
                <el-button @click="dialogVisible = false">取消</el-button>
                <el-button type="primary" @click="handleSubmit">确定</el-button>
            </template>
        </el-dialog>
    </div>
</template>

<style scoped>
.task-list-container {
    height: 100%;
    display: flex;
    flex-direction: column;
    background: #f2f3f5;
}

.content-wrapper {
    flex: 1;
    padding: 20px;
    background: #fff;
    margin: 10px;
    border-radius: 4px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
}

.toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;
    flex-wrap: wrap;
    gap: 15px;
}

.left-tools, .right-tools {
    display: flex;
    align-items: center;
    gap: 10px;
}

.search-input {
    width: 220px;
}

.task-name {
    font-weight: 500;
    color: #409eff;
    cursor: pointer;
}
.task-name:hover {
    text-decoration: underline;
}

.schedule-text {
    font-size: 13px;
    color: #303133;
}

.text-gray { color: #909399; }

.task-form {
    padding: 10px 20px 0 0;
}

.unit-label {
    color: #909399;
    font-size: 13px;
}

.week-checkbox .el-checkbox-button {
    margin-bottom: 0;
}
</style>
