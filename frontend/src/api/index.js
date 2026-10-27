import axios from 'axios'
import { normalizeError } from '@/utils/errors'
import { versionRequestConfig } from '@/utils/versionToken'
import { createSessionExpiredController, SESSION_EXPIRED_MESSAGE, TOKEN_STORAGE_KEY } from '@/utils/sessionExpiry'

export const LOGIN_NOTICE_KEY = 'autodroid.login_notice'

const api = axios.create({
    baseURL: '/api', // Proxy handles this
    timeout: 30000  // 30 seconds default
})

// 轮询类 GET 的显式短超时：闲置/劣化网络下慢请求快速失败，避免 pending XHR
// 堆积打满同域连接池把点击请求饿死。全局默认 30s 保留；这些方法可在调用处
// 用 config.timeout 覆盖（如确有更长的合法慢请求）。
const POLL_GET_TIMEOUT_MS = 8000

// 一次性大载荷 GET（报告/场景详情等首载）显式用全局 30s：8s 短超时只留给
// 真轮询，不再误伤一次性的大响应首载。
const DETAIL_GET_TIMEOUT_MS = 30000

const buildReportAssetUrl = (path) => {
    const normalized = String(path || '').trim().replace(/^\/+/, '')
    const encodedPath = normalized.split('/').map(encodeURIComponent).join('/')
    return encodedPath ? `/api/report-assets/${encodedPath}` : ''
}

// window.desktop preload bridge (Electron). Missing in pure web mode, where every
// desktop helper below degrades to a no-op / null so call sites never branch on it.
const desktopBridge = () => {
    if (typeof window === 'undefined') return null
    return window.desktop || null
}

// Manual sign-out flag: marks the saved login (loggedOut=true, autoLogin cleared) so
// auto-login stays off across restarts until the user logs in again with 自动登录.
// Bridge method is markLoggedOut (IPC desktop:mark-logged-out); when it is absent or
// fails this degrades to a silent no-op — logout is never blocked by it.
// manual=true (传参) marks the user-initiated sign-out; the flag is only ever set
// explicitly, never inferred. The preload's markLoggedOut() is zero-arg.
const markLoggedLogout = (manual = true) => {
    const bridge = desktopBridge()
    if (!bridge?.markLoggedOut || manual !== true) return Promise.resolve()
    return Promise.resolve().then(() => bridge.markLoggedOut()).catch(() => {})
}

// ===== 会话过期（401）单飞处理 =====
// 闲置劣化后在途请求会成批打回 401：无单飞门闩时每发一个 401 都弹一框、跳一次。
// 这里模块级 redirecting 单飞，只处理首个；「会话过期」处理器可注入
// （setSessionExpiredHandler，默认 ElMessageBox.confirm「登录状态已过期，请重新登录」
// → 用户点击后 router.push('/login')，用户手势内跳转绕开桌面壳吞程序化跳转）；
// 状态清理统一经 useUserStore.logout()（pinia token 与 localStorage 一并清）。
// 控制器惰性创建、清理用动态 import，避免与 useUserStore/router 形成模块环。
let sessionExpiredController = null
let sessionExpiredHandler = null

// 默认状态清理：经 useUserStore.logout() 统一清 pinia token 与 localStorage。
const clearSessionOnExpiry = async () => {
    try {
        const { useUserStore } = await import('@/stores/useUserStore')
        useUserStore().logout()
    } catch {
        try {
            localStorage.removeItem(TOKEN_STORAGE_KEY)
        } catch { /* 无可清时忽略 */ }
    }
}

const getSessionExpiredController = () => {
    if (!sessionExpiredController) {
        sessionExpiredController = createSessionExpiredController({
            // 显式传存储：控制器的「未登录不弹框」判定与请求门闩同一份 token
            storage: typeof window !== 'undefined' ? window.localStorage : null,
            handler: sessionExpiredHandler,
            logout: clearSessionOnExpiry,
        })
    }
    return sessionExpiredController
}

// 注入「会话过期」处理器（测试/自定义 UI）：只替换呈现与跳转，单飞与清理不变；
// 传非函数恢复默认弹框流程。
export const setSessionExpiredHandler = (handler) => {
    sessionExpiredHandler = typeof handler === 'function' ? handler : null
    sessionExpiredController?.setHandler(sessionExpiredHandler)
}

// 重置单飞状态与注入（测试隔离用）。
export const resetSessionExpired = () => {
    sessionExpiredHandler = null
    sessionExpiredController = null
}

// 预登录端点（登录/注册/客户端模式探测）：无 token 也照常放行。
const PRE_LOGIN_PATHS = ['/auth/token', '/auth/register', '/auth/registration-status', '/client/info']

const isPreLoginRequest = (url) => {
    const path = String(url || '').split('?')[0].replace(/\/+$/, '') || '/'
    return PRE_LOGIN_PATHS.some(p => path === p)
}

// 无 token 时的本地快速拒绝：会话已结束（401 处理/手动登出），轮询/后台请求
// 「发起前」即停，不再打网络堆积连接池；中文 message 让提示层不露英文。
const createSessionExpiredAbort = (config) => {
    const error = new Error(SESSION_EXPIRED_MESSAGE)
    error.code = 'ERR_SESSION_EXPIRED'
    error.sessionExpired = true
    error.config = config
    return error
}

// Request interceptor
api.interceptors.request.use(
    (config) => {
        const token = localStorage.getItem('token')
        if (token) {
            config.headers.Authorization = `Bearer ${token}`
        } else if (!isPreLoginRequest(config.url)) {
            // 401/登出后 token 已清：发起前本地拒绝，轮询自动静默停止
            throw createSessionExpiredAbort(config)
        }
        return config
    },
    (error) => {
        return Promise.reject(error)
    }
)

// Response interceptor
api.interceptors.response.use(
    (response) => {
        return response
    },
    (error) => {
        // Every failure leaves here with Chinese `message` and `data.detail`,
        // so call sites that print either never show axios/English text.
        normalizeError(error)
        if (error.response && error.response.status === 401 && !error.config?.silent401) {
            // 会话过期：单飞处理（弹框 + useUserStore.logout() 清态 + 确认后
            // router.push('/login')），不再 window.location.href 硬跳（桌面壳
            // 会吞跳转且无单飞时连环跳）；未登录时处理器静默放行。
            // silent401（路由守卫取 userInfo 的请求）不弹框：由守卫静默收口
            // /login，弹框只服务会话中途的后台 401（取消=留在当前页仍有效）。
            getSessionExpiredController().handleSessionExpired()
        }
        return Promise.reject(error)
    }
)

export default {
    // Typed, device-independent HTTP automation. Uses the same auth interceptors.
    apiTesting: {
        // 一次性 GET（详情/首载大载荷）：默认全局 30s；真轮询调用处显式传
        // pollRequestConfig() 用 8s 短超时（8s 只留给真轮询）
        get: (path, params, config) => api.get(`/api-testing${path}`, { timeout: DETAIL_GET_TIMEOUT_MS, params, ...config }),
        post: (path, data, config) => api.post(`/api-testing${path}`, data, config),
        put: (path, data) => api.put(`/api-testing${path}`, data),
        patch: (path, data) => api.patch(`/api-testing${path}`, data),
        delete: path => api.delete(`/api-testing${path}`),
        download: id => api.get(`/api-testing/runs/${id}/download`, { responseType: 'blob' }),
    },
    // Auth
    // options.server: 客户端（桌面引擎）模式下的上游服务器地址，随 X-AutoDroid-Server
    // 头交给本地引擎归一化后反代；纯 Web 模式不传该头，行为不变。
    login(data, options = {}) {
        const config = {}
        const server = typeof options?.server === 'string' ? options.server.trim() : ''
        if (server) config.headers = { 'X-AutoDroid-Server': server }
        return api.post('/auth/token', data, config)
    },
    register(data) {
        return api.post('/auth/register', data)
    },
    getRegistrationStatus() {
        return api.get('/auth/registration-status')
    },
    // config.silent401：该请求的 401 不触发「会话过期」弹框（路由守卫静默收口用）
    getUserInfo(config = {}) {
        return api.get('/auth/users/me', config)
    },
    changePassword(data) {
        return api.put('/auth/password', data)
    },

    // Desktop client (local engine): GET /api/client/info returns 200 with
    // {clientMode:true,...} only in the local engine; 404/transport failure = web mode.
    getClientInfo() {
        return api.get('/client/info')
    },
    // Saved login (window.desktop preload). Returns null when absent or unbridged.
    getSavedLogin() {
        const bridge = desktopBridge()
        return bridge?.getSavedLogin ? Promise.resolve(bridge.getSavedLogin()) : Promise.resolve(null)
    },
    // data=null clears the saved login; password is only handed to the preload,
    // which encrypts it with safeStorage before anything touches disk.
    saveLogin(data) {
        const bridge = desktopBridge()
        return bridge?.saveLogin ? Promise.resolve(bridge.saveLogin(data)) : Promise.resolve()
    },
    // Manual sign-out flag → bridge markLoggedOut (IPC desktop:mark-logged-out).
    // Silent no-op when the bridge method is missing or fails (see markLoggedLogout).
    markLoggedLogout,
    // Alias for the `api.markLoggedOut()` spelling; same behavior.
    markLoggedOut: markLoggedLogout,

    // Cases
    getTestCases(params) {
        return api.get('/cases/', { params })
    },
    getTestCase(id) {
        return api.get(`/cases/${id}`)
    },
    createTestCase(data) {
        return api.post('/cases/', data)
    },
    runTestCase(id) {
        // Sync run (deprecated for long runs but kept for simple ones)
        return api.post(`/cases/${id}/run`, null, { timeout: 300000 })
    },
    runTestCaseAsync(id, envId, deviceSerial) {
        let qs = []
        if (envId) qs.push(`env_id=${envId}`)
        if (deviceSerial) qs.push(`device_serial=${deviceSerial}`)
        const qsStr = qs.length ? `?${qs.join('&')}` : ''
        return api.post(`/cases/${id}/run${qsStr}`)
    },
    runTestCaseBatch(id, envId, deviceSerials) {
        return api.post(`/cases/${id}/run-batch`, {
            env_id: envId || null,
            device_serials: Array.isArray(deviceSerials) ? deviceSerials : (deviceSerials ? [deviceSerials] : [])
        })
    },
    precheckTestCase(id, envId, deviceSerial) {
        let qs = []
        if (envId) qs.push(`env_id=${envId}`)
        if (deviceSerial) qs.push(`device_serial=${deviceSerial}`)
        const qsStr = qs.length ? `?${qs.join('&')}` : ''
        return api.get(`/cases/${id}/precheck${qsStr}`)
    },
    deleteTestCase(id) {
        return api.delete(`/cases/${id}`)
    },
    duplicateTestCase(id) {
        return api.post(`/cases/${id}/duplicate`)
    },
    updateTestCase(id, data) {
        return api.put(`/cases/${id}`, data)
    },
    getCaseStandardSteps(id) {
        return api.get(`/cases/${id}/steps`)
    },
    // 步骤全量替换带乐观锁令牌（If-Match + version/expected_updated_at），
    // 并发修改服务端返回 409，调用方提示刷新重拉而不是静默覆盖。
    replaceCaseStandardSteps(id, steps, versionState = null) {
        return api.put(`/cases/${id}/steps`, steps, versionRequestConfig(versionState))
    },

    // Folders
    getFolderTree() {
        return api.get('/folders/tree')
    },
    createFolder(data) {
        return api.post('/folders/', data)
    },
    renameFolder(id, data) {
        return api.put(`/folders/${id}`, data)
    },
    deleteFolder(id) {
        return api.delete(`/folders/${id}`)
    },
    moveCase(caseId, folderId) {
        return api.patch(`/folders/move-case/${caseId}`, { folder_id: folderId })
    },

    // Scenario Folders
    getScenarioFolderTree() {
        return api.get('/scenario-folders/tree')
    },
    createScenarioFolder(data) {
        return api.post('/scenario-folders/', data)
    },
    renameScenarioFolder(id, data) {
        return api.put(`/scenario-folders/${id}`, data)
    },
    deleteScenarioFolder(id) {
        return api.delete(`/scenario-folders/${id}`)
    },
    moveScenario(scenarioId, folderId) {
        return api.patch(`/scenario-folders/move-scenario/${scenarioId}`, { folder_id: folderId })
    },

    // Device
    getDeviceDump(serial, options = {}) {
        const signal = options?.signal
        const params = {}
        if (serial) params.serial = serial
        if (Object.prototype.hasOwnProperty.call(options, 'includeDeviceInfo')) {
            params.include_device_info = options.includeDeviceInfo
        }
        if (Object.prototype.hasOwnProperty.call(options, 'includeHierarchy')) {
            params.include_hierarchy = options.includeHierarchy
        }
        if (Object.prototype.hasOwnProperty.call(options, 'includeScreenshot')) {
            params.include_screenshot = options.includeScreenshot
        }
        const requestConfig = { params }
        if (signal) {
            requestConfig.signal = signal
        }
        return api.get('/device/dump', requestConfig)
    },
    inspectDevice(x, y, serial) {
        const query = serial ? `&serial=${serial}` : ''
        return api.post(`/device/inspect?x=${x}&y=${y}${query}`)
    },
    interactDevice(x, y, operation, xml_dump, action_data, serial, record_step = true, include_screenshot = true) {
        return api.post('/device/interact', { x, y, operation, xml_dump, action_data, device_serial: serial, record_step, include_screenshot })
    },
    executeStep(step) {
        return api.post('/device/execute_step', step)
    },
    cropTemplate(screenshot_base64, x1, y1, x2, y2) {
        return api.post('/device/crop_template', { screenshot_base64, x1, y1, x2, y2 })
    },

    getScenarios(params) {
        return api.get('/scenarios/', { params })
    },
    createScenario(data) {
        return api.post('/scenarios/', data)
    },
    getScenario(id) {
        return api.get(`/scenarios/${id}`)
    },
    getScenarioSteps(id) {
        return api.get(`/scenarios/${id}/steps`)
    },
    // 场景步骤替换与用例步骤同一乐观锁契约（versionState 见 utils/versionToken.js）。
    updateScenarioSteps(id, steps, versionState = null) {
        return api.post(`/scenarios/${id}/steps`, steps, versionRequestConfig(versionState))
    },
    updateScenario(id, data) {
        return api.put(`/scenarios/${id}`, data)
    },
    deleteScenario(id) {
        return api.delete(`/scenarios/${id}`)
    },
    runScenario(id, envId, deviceSerials) {
        return api.post(`/scenarios/${id}/run`, {
            env_id: envId || null,
            device_serials: Array.isArray(deviceSerials) ? deviceSerials : (deviceSerials ? [deviceSerials] : [])
        })
    },
    precheckScenario(id, envId, deviceSerial) {
        let qs = []
        if (envId) qs.push(`env_id=${envId}`)
        if (deviceSerial) qs.push(`device_serial=${deviceSerial}`)
        const qsStr = qs.length ? `?${qs.join('&')}` : ''
        return api.get(`/scenarios/${id}/precheck${qsStr}`)
    },

    // Scrcpy 设备流
    getDevices(config = {}) {
        return api.get('/stream/devices', { timeout: POLL_GET_TIMEOUT_MS, ...config })
    },
    getDevice(serial) {
        return api.get(`/stream/devices/${serial}`)
    },
    reconnectDeviceStream(serial) {
        return api.post(`/stream/devices/${serial}/reconnect`)
    },
    getStreamProfile(serial) {
        return api.get(`/stream/devices/${serial}/stream-profile`)
    },
    setStreamProfile(serial, profile) {
        return api.post(`/stream/devices/${serial}/stream-profile`, { profile })
    },
    sendTouch(serial, action, x, y, options = {}) {
        const payload = { action, x, y }
        if (options.method) {
            payload.method = options.method
        }
        return api.post(`/stream/devices/${serial}/touch`, payload)
    },

    // Reports
    getReports(params) {
        return api.get('/reports/executions', { params })
    },
    getReport(id) {
        return api.get(`/reports/executions/${id}`)
    },
    getReportDownloadUrl(id) {
        return `/api/reports/executions/${id}/download`
    },
    getReportAssetUrl(path) {
        return buildReportAssetUrl(path)
    },
    getDashboardStats() {
        return api.get('/reports/dashboard/stats')
    },
    getFlakyAnalysis(params) {
        return api.get('/reports/flaky', { params })
    },
    compareExecutions(baseId, targetId) {
        return api.get('/reports/executions/compare', { params: { base_id: baseId, target_id: targetId } })
    },
    getDashboardOverview(params, config = {}) {
        return api.get('/reports/dashboard/overview', { timeout: POLL_GET_TIMEOUT_MS, params, ...config })
    },
    deleteExecution(id) {
        return api.delete(`/reports/executions/${id}`)
    },
    deleteBatch(batchId) {
        return api.delete(`/reports/batch/${batchId}`)
    },
    cancelRun(data) {
        return api.post('/runs/cancel', data)
    },
    getActiveRuns(kind, targetId, config = {}) {
        return api.get('/runs/active', { timeout: POLL_GET_TIMEOUT_MS, params: { kind, target_id: targetId }, ...config })
    },
    // Limiter (执行限流/排队)
    getLimiterStats() {
        return api.get('/limiter/stats')
    },

    // Tasks (定时任务)
    getTasks(params) {
        return api.get('/tasks/', { params })
    },
    createTask(data) {
        return api.post('/tasks/', data)
    },
    updateTask(id, data) {
        return api.put(`/tasks/${id}`, data)
    },
    toggleTask(id) {
        return api.patch(`/tasks/${id}/toggle`)
    },
    deleteTask(id) {
        return api.delete(`/tasks/${id}`)
    },

    // Settings (系统配置)
    getSettings() {
        return api.get('/settings/')
    },
    getFeatureFlags() {
        return api.get('/settings/feature-flags')
    },
    getAssetStorageStatus() {
        return api.get('/assets/status')
    },
    saveSettings(items) {
        return api.post('/settings/', items)
    },
    sendTestNotification(webhookUrl) {
        return api.post('/settings/test-notification', { webhook_url: webhookUrl })
    },

    // API Tokens (CI 集成机器凭证)
    getApiTokens(params) {
        return api.get('/tokens/', { params })
    },
    createApiToken(name) {
        return api.post('/tokens/', { name })
    },
    revokeApiToken(id) {
        return api.delete(`/tokens/${id}`)
    },

    // Admin (用户管理)
    getAdminRegistrationSettings() {
        return api.get('/admin/registration-settings')
    },
    updateAdminRegistrationSettings(allowRegistration) {
        return api.put('/admin/registration-settings', { allow_registration: allowRegistration })
    },
    getAdminUsers(params) {
        return api.get('/admin/users', { params })
    },
    createAdminUser(data) {
        return api.post('/admin/users', data)
    },
    updateAdminUserStatus(id, isActive) {
        return api.patch(`/admin/users/${id}/status`, { is_active: isActive })
    },

    // Fastbot (性能测试)
    getFastbotTasks(params, config = {}) {
        return api.get('/fastbot/tasks', { timeout: POLL_GET_TIMEOUT_MS, params, ...config })
    },
    getFastbotTask(id) {
        return api.get(`/fastbot/tasks/${id}`)
    },
    deleteFastbotTask(id) {
        return api.delete(`/fastbot/tasks/${id}`)
    },
    runFastbot(data) {
        return api.post('/fastbot/run', data)
    },
    getFastbotReport(taskId) {
        return api.get(`/fastbot/reports/${taskId}`)
    },
    getFastbotReplay(taskId, filename) {
        return api.get(`/fastbot/replays/${taskId}/${encodeURIComponent(filename)}`, {
            responseType: 'blob',
            timeout: 120000,
        })
    },
    getFluencySessions(params, config = {}) {
        return api.get('/fastbot/fluency/sessions', { timeout: POLL_GET_TIMEOUT_MS, params, ...config })
    },
    getStartupTasks(params, config = {}) {
        return api.get('/fastbot/startup/tasks', { timeout: POLL_GET_TIMEOUT_MS, params, ...config })
    },
    runStartupTest(data) {
        return api.post('/fastbot/startup/run', data)
    },
    startFluencySession(data) {
        return api.post('/fastbot/fluency/start', data)
    },
    addFluencyMarker(taskId, data) {
        return api.post(`/fastbot/fluency/${taskId}/markers`, data)
    },
    stopFluencySession(taskId) {
        return api.post(`/fastbot/fluency/${taskId}/stop`)
    },
    analyzeFastbotTrace(taskId, data) {
        return api.post(`/fastbot/reports/${taskId}/trace_ai_summary`, data, { timeout: 120000 })
    },
    getFastbotDevices() {
        return api.get('/fastbot/devices')
    },

    // Compatibility (视觉兼容性)
    getCompatPageSets() {
        return api.get('/compatibility/page-sets')
    },
    createCompatPageSet(data) {
        return api.post('/compatibility/page-sets', data)
    },
    updateCompatPageSet(id, data) {
        return api.put(`/compatibility/page-sets/${id}`, data)
    },
    deleteCompatPageSet(id) {
        return api.delete(`/compatibility/page-sets/${id}`)
    },
    getCompatibilityRuns(params, config = {}) {
        return api.get('/compatibility/runs', { timeout: POLL_GET_TIMEOUT_MS, params, ...config })
    },
    createCompatibilityRun(data) {
        return api.post('/compatibility/runs', data)
    },
    preflightCompatibilityReplay(data) {
        return api.post('/compatibility/replay-preflight', data, { timeout: 60000 })
    },
    getCompatibilityRun(id, config = {}) {
        // 报告详情一次性大载荷：全局 30s（8s 只留给真轮询）
        return api.get(`/compatibility/runs/${id}`, { timeout: DETAIL_GET_TIMEOUT_MS, ...config })
    },
    getCompatibilityPageDiff(runId, pageResultId) {
        return api.get(`/compatibility/runs/${runId}/pages/${pageResultId}/diff`, {
            timeout: 60000,
        })
    },
    deleteCompatibilityRun(id) {
        return api.delete(`/compatibility/runs/${id}`)
    },
    cancelCompatibilityRun(id) {
        return api.post(`/compatibility/runs/${id}/cancel`)
    },

    // Model inspection
    getInspectionProfiles() {
        return api.get('/inspections/profiles')
    },
    createInspectionProfile(data) {
        return api.post('/inspections/profiles', data)
    },
    updateInspectionProfile(id, data) {
        return api.put(`/inspections/profiles/${id}`, data)
    },
    deleteInspectionProfile(id) {
        return api.delete(`/inspections/profiles/${id}`)
    },
    createInspectionRun(data) {
        return api.post('/inspections/runs', data)
    },
    getInspectionRuns(params, config = {}) {
        return api.get('/inspections/runs', { timeout: POLL_GET_TIMEOUT_MS, params, ...config })
    },
    getInspectionRun(id, config = {}) {
        // 报告详情一次性大载荷：全局 30s（8s 只留给真轮询）
        return api.get(`/inspections/runs/${id}`, { timeout: DETAIL_GET_TIMEOUT_MS, ...config })
    },
    deleteInspectionRun(id) {
        return api.delete(`/inspections/runs/${id}`)
    },
    cancelInspectionRun(id) {
        return api.post(`/inspections/runs/${id}/cancel`)
    },
    getInspectionGraph(id, params = {}) {
        return api.get(`/inspections/runs/${id}/graph`, { params })
    },
    getInspectionFamilies(id, params = {}) {
        return api.get(`/inspections/runs/${id}/families`, { params })
    },
    createInspectionLiveSession(id) {
        return api.post(`/inspections/runs/${id}/live-session`)
    },
    getInspectionLive(id, config = {}) {
        return api.get(`/inspections/runs/${id}/live`, { timeout: POLL_GET_TIMEOUT_MS, ...config })
    },
    getInspectionActionMap(id, stateId) {
        return api.get(`/inspections/runs/${id}/states/${stateId}/action-map`)
    },
    getInspectionObservations(id, stateId, params = {}) {
        return api.get(`/inspections/runs/${id}/states/${stateId}/observations`, { params })
    },
    updateInspectionRepresentative(id, stateId, observationId) {
        return api.put(`/inspections/runs/${id}/states/${stateId}/representative-observation`, {
            observation_id: observationId,
        })
    },
    getInspectionLiveAsset(url, responseType = 'blob') {
        const parsed = new URL(String(url || ''), window.location.origin)
        if (parsed.origin !== window.location.origin || !parsed.pathname.startsWith('/api/inspections/')) {
            return Promise.reject(new Error('巡检实时资产地址无效'))
        }
        const requestPath = `${parsed.pathname.replace(/^\/api/, '')}${parsed.search}`
        return api.get(requestPath, { responseType, timeout: 60000 })
    },
    updateInspectionSelection(id, stateIds, observationIds = []) {
        return api.put(`/inspections/runs/${id}/regression-selection`, {
            state_ids: stateIds,
            observation_ids: observationIds,
        })
    },
    getAsset(assetId, responseType = 'blob', options = {}) {
        if (assetId === null || assetId === undefined || assetId === '') {
            return Promise.reject(new Error('资产 ID 无效'))
        }
        const headers = { ...(options.headers || {}) }
        if (options.range) headers.Range = options.range
        return api.get(`/assets/${encodeURIComponent(String(assetId))}`, {
            responseType,
            timeout: options.timeout || 60000,
            headers,
        })
    },
    getInspectionAsset(id, path, responseType = 'text') {
        return api.get(`/inspections/runs/${id}/assets`, {
            params: { path },
            responseType,
            timeout: 60000,
        })
    },

    // Log Analysis (AI 智能分析)
    analyzeLog(data) {
        return api.post('/fastbot/analyze_log', data, { timeout: 120000 })
    },

    // Device Management (设备管理)
    getDeviceList(options = {}) {
        const params = {}
        if (Object.prototype.hasOwnProperty.call(options, 'refreshIosWda')) {
            params.refresh_ios_wda = options.refreshIosWda
        }
        // 设备列表是 2s 级轮询目标：默认短超时，options.timeout 可覆盖
        return api.get('/devices/', {
            timeout: options.timeout ?? POLL_GET_TIMEOUT_MS,
            params
        })
    },
    syncDevices() {
        return api.post('/devices/sync')
    },
    bindAppDevice(serial, physicalSerial) {
        return api.post(`/devices/${encodeURIComponent(serial)}/app-binding`, { physical_serial: physicalSerial || null })
    },
    getDeviceScreenshot(serial) {
        return api.get(`/devices/${serial}/screenshot`, { timeout: 30000 })
    },
    unlockDevice(serial) {
        return api.post(`/devices/${serial}/unlock`)
    },
    stopDeviceExecution(serial) {
        return api.post(`/devices/${serial}/unlock`)
    },
    rebootDevice(serial) {
        return api.post(`/devices/${serial}/reboot`)
    },
    checkDeviceWda(serial) {
        return api.post(`/devices/${serial}/wda/check`)
    },
    enableDeviceWireless(serial, payload = {}) {
        return api.post(`/devices/${serial}/wireless/enable`, payload)
    },
    disableDeviceWireless(serial) {
        return api.post(`/devices/${serial}/wireless/disable`)
    },
    renameDevice(serial, customName) {
        return api.put(`/devices/${serial}/name`, { custom_name: customName })
    },
    deleteDevice(serial) {
        return api.delete(`/devices/${serial}`)
    },

    // Device Agents (远程设备接入点)
    getDeviceAgents() {
        return api.get('/device-agents/')
    },
    probeDeviceAgentLink(agentId) {
        return api.post(`/device-agents/${agentId}/link-probe`, null, { timeout: 30000 })
    },
    getDeviceTransportMetrics(serial) {
        return api.get(`/devices/${serial}/transport-metrics`)
    },
    deleteDeviceAgent(agentId) {
        return api.delete(`/device-agents/${agentId}`)
    },
    downloadDeviceAgentScript() {
        return api.get('/device-agents/agent-script', { responseType: 'blob', timeout: 60000 })
    },

    // App Packages (包管理)
    getPackages(params) {
        return api.get('/packages/', { params })
    },
    createPackageUploadSession(data) {
        return api.post('/packages/upload-sessions', data)
    },
    uploadPackageChunk(uploadId, index, formData, config = {}) {
        return api.post(`/packages/upload-sessions/${uploadId}/chunks/${index}`, formData, {
            timeout: 300000,
            ...config,
            headers: {
                ...(config.headers || {}),
                'Content-Type': 'multipart/form-data'
            }
        })
    },
    completePackageUpload(uploadId) {
        return api.post(`/packages/upload-sessions/${uploadId}/complete`, null, { timeout: 300000 })
    },
    cancelPackageUpload(uploadId) {
        return api.delete(`/packages/upload-sessions/${uploadId}`)
    },
    deletePackage(id) {
        return api.delete(`/packages/${id}`)
    },
    getPackageDownloadUrl(id) {
        return `/api/packages/${id}/download`
    },
    installPackage(id, serial) {
        return api.post(`/packages/${id}/install`, { serial }, { timeout: 480000 })
    },

    // Environments (全局变量库)
    getEnvironments() {
        return api.get('/environments/')
    },
    createEnvironment(data) {
        return api.post('/environments/', data)
    },
    updateEnvironment(id, data) {
        return api.put(`/environments/${id}`, data)
    },
    deleteEnvironment(id) {
        return api.delete(`/environments/${id}`)
    },
    getVariables(envId) {
        return api.get(`/environments/${envId}/variables`)
    },
    createVariable(envId, data) {
        return api.post(`/environments/${envId}/variables`, data)
    },
    updateVariable(varId, data) {
        return api.put(`/environments/variables/${varId}`, data)
    },
    deleteVariable(varId) {
        return api.delete(`/environments/variables/${varId}`)
    },

    // AI (NL2Script) - 自然语言生成测试步骤
    generateAISteps(text) {
        return api.post('/ai/generate-steps', { text }, { timeout: 120000 })
    },

    // AutoDroid 脚本导出与设备端执行任务
    autojs: {
        precheckCase: (id, envId, platform) => api.get(`/autojs/cases/${id}/precheck`, { params: { ...(envId ? { env_id: envId } : {}), ...(platform ? { platform } : {}) } }),
        exportCase: (id, format = 'zip', envId, platform) => api.post(`/autojs/cases/${id}/export`, null, {
            params: { format, ...(envId ? { env_id: envId } : {}), ...(platform ? { platform } : {}) },
            responseType: 'blob',
            timeout: 120000
        }),
        exportBatch: (data) => api.post('/autojs/cases/export-batch', data, { responseType: 'blob', timeout: 120000 }),
        precheckScenario: (id, envId, platform) => api.get(`/autojs/scenarios/${id}/precheck`, { params: { ...(envId ? { env_id: envId } : {}), ...(platform ? { platform } : {}) } }),
        exportScenario: (id, envId, platform) => api.post(`/autojs/scenarios/${id}/export`, null, {
            params: { ...(envId ? { env_id: envId } : {}), ...(platform ? { platform } : {}) },
            responseType: 'blob',
            timeout: 120000
        }),
        createTask: (data) => api.post('/autojs/tasks', data),
        listTasks: (params) => api.get('/autojs/tasks', { params }),
        cancelTask: (taskId) => api.post(`/autojs/tasks/${taskId}/cancel`)
    }
}
