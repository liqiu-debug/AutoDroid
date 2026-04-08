import axios from 'axios'

const api = axios.create({
    baseURL: '/api', // Proxy handles this
    timeout: 30000  // 30 seconds default
})

const buildReportAssetUrl = (path) => {
    const normalized = String(path || '').trim().replace(/^\/+/, '')
    return normalized ? `/api/report-assets/${encodeURI(normalized)}` : ''
}

// Request interceptor
api.interceptors.request.use(
    (config) => {
        const token = localStorage.getItem('token')
        if (token) {
            config.headers.Authorization = `Bearer ${token}`
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
        if (error.response && error.response.status === 401) {
            localStorage.removeItem('token')
            window.location.href = '/login'
        }
        return Promise.reject(error)
    }
)

export default {
    // Auth
    login(data) {
        return api.post('/auth/token', data)
    },
    register(data) {
        return api.post('/auth/register', data)
    },
    getUserInfo() {
        return api.get('/auth/users/me')
    },

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
    replaceCaseStandardSteps(id, steps) {
        return api.put(`/cases/${id}/steps`, steps)
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

    // Device
    getDeviceDump(serial, options = {}) {
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
        return api.get('/device/dump', { params })
    },
    inspectDevice(x, y, serial) {
        const query = serial ? `&serial=${serial}` : ''
        return api.post(`/device/inspect?x=${x}&y=${y}${query}`)
    },
    interactDevice(x, y, operation, xml_dump, action_data, serial, record_step = true) {
        return api.post('/device/interact', { x, y, operation, xml_dump, action_data, device_serial: serial, record_step })
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
    updateScenarioSteps(id, steps) {
        return api.post(`/scenarios/${id}/steps`, steps)
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
    getDevices() {
        return api.get('/stream/devices')
    },
    getDevice(serial) {
        return api.get(`/stream/devices/${serial}`)
    },
    sendTouch(serial, action, x, y) {
        return api.post(`/stream/devices/${serial}/touch`, { action, x, y })
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
    getDashboardOverview(params) {
        return api.get('/reports/dashboard/overview', { params })
    },

    // Tasks (定时任务)
    getTasks() {
        return api.get('/tasks/')
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
    saveSettings(items) {
        return api.post('/settings/', items)
    },
    sendTestNotification(webhookUrl) {
        return api.post('/settings/test-notification', { webhook_url: webhookUrl })
    },

    // Fastbot (性能测试)
    getFastbotTasks(params) {
        return api.get('/fastbot/tasks', { params })
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
    getFluencySessions(params) {
        return api.get('/fastbot/fluency/sessions', { params })
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

    // Log Analysis (AI 智能分析)
    analyzeLog(data) {
        return api.post('/fastbot/analyze_log', data, { timeout: 120000 })
    },

    // Device Management (设备管理)
    getDeviceList() {
        return api.get('/devices/')
    },
    syncDevices() {
        return api.post('/devices/sync')
    },
    getDeviceScreenshot(serial) {
        return api.get(`/devices/${serial}/screenshot`, { timeout: 30000 })
    },
    unlockDevice(serial) {
        return api.post(`/devices/${serial}/unlock`)
    },
    rebootDevice(serial) {
        return api.post(`/devices/${serial}/reboot`)
    },
    checkDeviceWda(serial) {
        return api.post(`/devices/${serial}/wda/check`)
    },
    renameDevice(serial, customName) {
        return api.put(`/devices/${serial}/name`, { custom_name: customName })
    },

    // App Packages (包管理)
    getPackages(params) {
        return api.get('/packages/', { params })
    },
    deletePackage(id) {
        return api.delete(`/packages/${id}`)
    },
    getPackageDownloadUrl(id) {
        return `/api/packages/${id}/download`
    },
    installPackage(id, serial) {
        return api.post(`/packages/${id}/install`, { serial }, { timeout: 120000 })
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
    }
}
