import { defineStore } from 'pinia'
import { ref, computed, nextTick } from 'vue'
import api from '@/api'
import { ElMessage } from 'element-plus'
import { createUuid } from '@/utils/uuid'

const SELECTOR_TYPE_TO_BY = {
    resourceId: 'id',
    text: 'text',
    xpath: 'xpath',
    description: 'description',
    image: 'image'
}

const BY_TO_SELECTOR_TYPE = {
    id: 'resourceId',
    resourceid: 'resourceId',
    resource_id: 'resourceId',
    text: 'text',
    xpath: 'xpath',
    description: 'description',
    desc: 'description',
    image: 'image',
    label: 'text',
    name: 'text'
}

const SUPPORTED_PLATFORMS = ['android', 'ios']
const CROSS_PLATFORM_ACTIONS = [
    'click',
    'input',
    'wait_until_exists',
    'assert_text',
    'assert_image',
    'swipe',
    'sleep',
    'start_app',
    'stop_app',
    'back',
    'home',
    'click_image',
    'extract_by_ocr'
]

const VARIABLE_PLACEHOLDER_PATTERN = /\{\{\s*([A-Z0-9_]+)\s*\}\}/g

const MAX_RETRY_COUNT = 3

function normalizeRetryCount(value) {
    const retryCount = Number.parseInt(value, 10)
    if (!Number.isFinite(retryCount)) return 0
    return Math.min(Math.max(retryCount, 0), MAX_RETRY_COUNT)
}

function formatVariablePlaceholder(key) {
    return `{{${String(key || '').trim()}}}`
}

function normalizeVariablePlaceholders(value) {
    if (typeof value === 'string') {
        return value.replace(VARIABLE_PLACEHOLDER_PATTERN, (_, key) => formatVariablePlaceholder(key))
    }
    if (Array.isArray(value)) {
        return value.map((item) => normalizeVariablePlaceholders(item))
    }
    if (value && typeof value === 'object') {
        return Object.fromEntries(
            Object.entries(value).map(([key, item]) => [key, normalizeVariablePlaceholders(item)])
        )
    }
    return value
}

function defaultExecuteOnForAction(action) {
    const actionName = String(action || '').trim().toLowerCase()
    if (CROSS_PLATFORM_ACTIONS.includes(actionName)) {
        return ['android', 'ios']
    }
    return ['android']
}

function normalizeExecuteOn(executeOn) {
    const values = Array.isArray(executeOn) ? executeOn : ['android', 'ios']
    const normalized = []
    values.forEach((item) => {
        const platform = String(item || '').trim().toLowerCase()
        if (SUPPORTED_PLATFORMS.includes(platform) && !normalized.includes(platform)) {
            normalized.push(platform)
        }
    })
    return normalized.length ? normalized : ['android']
}

function normalizePlatformSelector(rawSelector) {
    if (!rawSelector || typeof rawSelector !== 'object') return null
    const selector = normalizeVariablePlaceholders(String(rawSelector.selector || '').trim())
    const by = String(rawSelector.by || '').trim().toLowerCase()
    if (!selector || !by) return null
    return { selector, by }
}

function ensureCrossPlatformFields(step) {
    const normalized = { ...(step || {}) }
    normalized.action = String(normalized.action || '').trim().toLowerCase()
    normalized.selector = normalizeVariablePlaceholders(normalized.selector)
    normalized.value = normalizeVariablePlaceholders(normalized.value)
    normalized.description = normalizeVariablePlaceholders(normalized.description)
    normalized.options = normalizeVariablePlaceholders(normalized.options)

    if (!normalized.uuid) {
        normalized.uuid = createUuid()
    }

    normalized.execute_on = defaultExecuteOnForAction(normalized.action)

    const overrides = (normalized.platform_overrides && typeof normalized.platform_overrides === 'object')
        ? normalized.platform_overrides
        : {}

    let android = normalizePlatformSelector(overrides.android)
    const ios = normalizePlatformSelector(overrides.ios)

    if (!android) {
        const selector = String(normalized.selector || '').trim()
        const selectorType = String(normalized.selector_type || '').trim()
        const by = SELECTOR_TYPE_TO_BY[selectorType]
        if (selector && by) {
            android = { selector, by }
        }
    }

    normalized.platform_overrides = {
        android,
        ios
    }

    if (['click_image', 'assert_image'].includes(normalized.action)) {
        normalized.selector_type = 'image'
    } else if (
        ['click', 'input', 'wait_until_exists'].includes(normalized.action)
        && !['text', 'description', 'xpath', 'resourceId'].includes(String(normalized.selector_type || '').trim())
    ) {
        normalized.selector_type = 'text'
    }

    if (!normalized.error_strategy) {
        normalized.error_strategy = 'ABORT'
    }

    normalized.retry_count = normalizeRetryCount(normalized.retry_count)

    const timeout = Number.parseInt(normalized.timeout, 10)
    normalized.timeout = Number.isFinite(timeout) && timeout > 0 ? timeout : 10

    if (normalized.action === 'sleep') {
        const seconds = Number.parseFloat(normalized.value)
        normalized.value = Number.isFinite(seconds) && seconds >= 1 ? String(seconds) : '5'
    }

    if (normalized.options && typeof normalized.options !== 'object') {
        normalized.options = {}
    }
    if (!normalized.options) {
        normalized.options = {}
    }
    if (normalized.action === 'assert_text') {
        normalized.options.match_mode = normalized.options.match_mode === 'not_contains'
            ? 'not_contains'
            : 'contains'
    }
    if (normalized.action === 'assert_image') {
        normalized.options.match_mode = normalized.options.match_mode === 'not_exists'
            ? 'not_exists'
            : 'exists'
    }

    return normalized
}

function standardStepToUiStep(step) {
    const action = String(step?.action || '').trim().toLowerCase()
    const args = (step && typeof step.args === 'object' && step.args) ? step.args : {}
    const overrides = (step && typeof step.platform_overrides === 'object' && step.platform_overrides)
        ? step.platform_overrides
        : {}
    const android = normalizePlatformSelector(overrides.android)

    let selector = android?.selector || ''
    let selectorType = android?.by ? (BY_TO_SELECTOR_TYPE[String(android.by).toLowerCase()] || null) : null
    let value = step?.value
    let options = {}

    if ((value === undefined || value === null || value === '') && action === 'input') {
        value = args.text
    }
    if ((value === undefined || value === null || value === '') && action === 'assert_text') {
        value = args.expected_text
    }
    if (action === 'click_image' || action === 'assert_image') {
        if (!selector) {
            selector = String(args.image_path || args.path || '')
        }
        selectorType = 'image'
    }
    if ((value === undefined || value === null || value === '') && action === 'sleep' && args.seconds !== undefined) {
        value = String(args.seconds)
    }

    if (action === 'swipe' && !selector) {
        selector = String(args.direction || 'up')
    }
    if ((action === 'start_app' || action === 'stop_app') && !selector) {
        selector = String(args.app_key || '')
    }
    if (action === 'assert_text') {
        selector = ''
        selectorType = null
        options.match_mode = args.match_mode === 'not_contains' ? 'not_contains' : 'contains'
    }
    if (action === 'assert_image') {
        options.match_mode = args.match_mode === 'not_exists' ? 'not_exists' : 'exists'
    }
    if (action === 'extract_by_ocr') {
        if (!selector && args.region !== undefined && args.region !== null) {
            selector = String(args.region)
        }
        if (args.extract_rule && typeof args.extract_rule === 'object') {
            options = args.extract_rule
        } else if (typeof args.extract_rule === 'string') {
            options = { extract_rule: args.extract_rule }
        }
        if (!options.extract_rule) {
            options.extract_rule = 'preset'
        }
    }

    return ensureCrossPlatformFields({
        uuid: step?.uuid,
        action,
        selector,
        selector_type: selectorType,
        value: value === undefined || value === null ? '' : String(value),
        options,
        description: step?.description || '',
        timeout: step?.timeout || 10,
        error_strategy: step?.error_strategy || 'ABORT',
        retry_count: normalizeRetryCount(step?.retry_count),
        execute_on: step?.execute_on,
        platform_overrides: action === 'assert_text' ? {} : (step?.platform_overrides || {})
    })
}

function isStandardStepPayload(step) {
    if (!step || typeof step !== 'object') return false
    if (step.args && typeof step.args === 'object') return true
    if (step.order !== undefined && !('selector' in step)) return true
    if ((step.execute_on !== undefined || step.platform_overrides !== undefined) && !('selector' in step || 'selector_type' in step)) {
        return true
    }
    return false
}

function uiStepToLegacyStep(step) {
    const normalized = ensureCrossPlatformFields(step)
    const action = String(normalized.action || '').trim().toLowerCase()
    const android = action === 'assert_text'
        ? null
        : normalizePlatformSelector(normalized.platform_overrides?.android)

    let selector = android?.selector !== undefined ? android.selector : (normalized.selector || '')
    let selectorType = android?.by
        ? (BY_TO_SELECTOR_TYPE[String(android.by).toLowerCase()] || normalized.selector_type || null)
        : (normalized.selector_type || null)

    // 非逻辑定位动作优先保留当前 selector（例如 extract_by_ocr 的区域）。
    if (!['click', 'input', 'wait_until_exists', 'assert_text', 'swipe', 'sleep'].includes(action)) {
        selector = String(normalized.selector || '').trim()
        if (action === 'click_image' || action === 'assert_image') {
            selectorType = 'image'
        } else if (!selectorType) {
            selectorType = 'text'
        }
    }
    if (action === 'assert_text') {
        selector = ''
        selectorType = null
    }

    return {
        uuid: normalized.uuid,
        action: normalized.action,
        selector,
        selector_type: selectorType,
        value: normalized.value === undefined || normalized.value === null ? '' : String(normalized.value),
        options: action === 'assert_text'
            ? {
                match_mode: normalized.options?.match_mode === 'not_contains' ? 'not_contains' : 'contains'
            }
            : action === 'assert_image'
                ? {
                    match_mode: normalized.options?.match_mode === 'not_exists' ? 'not_exists' : 'exists'
                }
                : (normalized.options && typeof normalized.options === 'object' ? normalized.options : {}),
        description: normalized.description || '',
        timeout: normalized.timeout,
        error_strategy: normalized.error_strategy || 'ABORT',
        retry_count: normalizeRetryCount(normalized.retry_count)
    }
}

function uiStepToStandardStep(step, order) {
    const normalized = ensureCrossPlatformFields(step)
    const action = String(normalized.action || '').trim().toLowerCase()

    const selectorType = String(normalized.selector_type || '').trim()
    const selector = String(normalized.selector || '').trim()
    const fallbackBy = SELECTOR_TYPE_TO_BY[selectorType]
    const fallbackAndroid = action === 'assert_text'
        ? null
        : (selector && fallbackBy ? { selector, by: fallbackBy } : null)

    let android = normalizePlatformSelector(normalized.platform_overrides?.android) || fallbackAndroid
    let ios = normalizePlatformSelector(normalized.platform_overrides?.ios)

    // 非逻辑定位动作（如 extract_by_ocr/click_image/start_app/stop_app）优先使用当前 selector。
    if (!['click', 'input', 'wait_until_exists', 'assert_text', 'swipe', 'sleep'].includes(action)) {
        if (selector) {
            android = {
                selector,
                by: fallbackBy || (['click_image', 'assert_image'].includes(action) ? 'image' : 'text')
            }
        }
    }
    if (action === 'assert_text') {
        android = null
        ios = null
    }

    const platform_overrides = {}
    if (android) platform_overrides.android = android
    if (ios) platform_overrides.ios = ios

    const args = (normalized.args && typeof normalized.args === 'object') ? { ...normalized.args } : {}
    if (action === 'input') {
        args.text = normalized.value === undefined || normalized.value === null ? '' : String(normalized.value)
    } else if (action === 'assert_text') {
        args.expected_text = normalized.value === undefined || normalized.value === null ? '' : String(normalized.value)
        args.match_mode = normalized.options?.match_mode === 'not_contains' ? 'not_contains' : 'contains'
    } else if (action === 'click_image') {
        args.image_path = selector
    } else if (action === 'assert_image') {
        args.image_path = selector
        args.match_mode = normalized.options?.match_mode === 'not_exists' ? 'not_exists' : 'exists'
    } else if (action === 'swipe') {
        args.direction = String(selector || 'up').toLowerCase()
    } else if (action === 'sleep') {
        const seconds = Number.parseFloat(normalized.value)
        args.seconds = Number.isFinite(seconds) && seconds >= 1 ? seconds : 5
    } else if (action === 'start_app' || action === 'stop_app') {
        args.app_key = selector
    } else if (action === 'extract_by_ocr') {
        args.region = selector
        args.extract_rule = normalized.options && typeof normalized.options === 'object' ? normalized.options : {}
    }

    return {
        order,
        action,
        args,
        value: normalized.value === undefined || normalized.value === null ? '' : String(normalized.value),
        execute_on: defaultExecuteOnForAction(action),
        platform_overrides,
        timeout: normalized.timeout,
        error_strategy: normalized.error_strategy || 'ABORT',
        retry_count: normalizeRetryCount(normalized.retry_count),
        description: normalized.description || ''
    }
}

export const useCaseStore = defineStore('case', () => {
    // State
    const currentCase = ref({
        id: null,
        name: '未命名用例',
        variables: [],
        steps: []
    })

    const caseList = ref([])
    const loading = ref(false)
    const running = ref(false)
    const lastAddedStepUuid = ref(null)
    const savedSnapshot = ref(null)

    function takeSnapshot() {
        return JSON.stringify({
            name: currentCase.value.name,
            steps: currentCase.value.steps,
            variables: currentCase.value.variables
        })
    }

    function updateSnapshot() {
        savedSnapshot.value = takeSnapshot()
    }

    // Getters
    const hasUnsavedChanges = computed(() => {
        if (savedSnapshot.value === null) return false
        return takeSnapshot() !== savedSnapshot.value
    })

    // Actions
    async function fetchCaseList() {
        loading.value = true
        try {
            const res = await api.getTestCases({ skip: 0, limit: 1000 })
            caseList.value = res.data.items || res.data
        } catch (err) {
            ElMessage.error('获取用例列表失败: ' + err.message)
        } finally {
            loading.value = false
        }
    }

    async function loadCase(id) {
        loading.value = true
        try {
            lastAddedStepUuid.value = null
            const res = await api.getTestCase(id)
            const caseData = res.data || {}
            let uiSteps = Array.isArray(caseData.steps)
                ? caseData.steps.map((step) => ensureCrossPlatformFields(step))
                : []

            try {
                const { data: standardSteps } = await api.getCaseStandardSteps(id)
                if (Array.isArray(standardSteps) && standardSteps.length > 0) {
                    uiSteps = standardSteps.map((step) => standardStepToUiStep(step))
                }
            } catch (stepErr) {
                console.warn('获取标准步骤失败，回退 legacy 步骤', stepErr)
            }

            currentCase.value = {
                ...caseData,
                variables: normalizeVariablePlaceholders(caseData.variables || []),
                steps: uiSteps
            }
            updateSnapshot()
            ElMessage.success('用例已加载')
        } catch (err) {
            ElMessage.error('加载用例失败: ' + err.message)
        } finally {
            loading.value = false
        }
    }

    async function saveCase() {
        loading.value = true
        try {
            const normalizedSteps = (currentCase.value.steps || []).map((step) => ensureCrossPlatformFields(step))
            const normalizedVariables = normalizeVariablePlaceholders(currentCase.value.variables || [])
            const legacySteps = normalizedSteps.map((step) => uiStepToLegacyStep(step))

            const payload = {
                ...currentCase.value,
                variables: normalizedVariables,
                steps: legacySteps
            }

            let res
            if (currentCase.value.id) {
                res = await api.updateTestCase(currentCase.value.id, payload)
            } else {
                res = await api.createTestCase(payload)
            }
            const savedCase = res.data || {}
            const savedCaseId = savedCase.id || currentCase.value.id
            let finalSteps = normalizedSteps

            if (savedCaseId) {
                try {
                    const standardPayload = normalizedSteps.map((step, index) => uiStepToStandardStep(step, index + 1))
                    const { data: savedStandardSteps } = await api.replaceCaseStandardSteps(savedCaseId, standardPayload)
                    if (Array.isArray(savedStandardSteps) && savedStandardSteps.length > 0) {
                        finalSteps = savedStandardSteps.map((step) => standardStepToUiStep(step))
                    }
                } catch (stepErr) {
                    console.error('保存标准步骤失败', stepErr)
                    ElMessage.warning('标准步骤保存失败，当前仅保存了兼容步骤。')
                }
            }

            currentCase.value = {
                ...savedCase,
                steps: finalSteps
            }
            // 延迟到 nextTick 拍快照，避免 StepBuilder 的 deep watcher
            // 在 Vue 调度刷新时突变 steps 导致快照与实际状态不一致
            nextTick(() => updateSnapshot())
            ElMessage.success('保存成功')
            await fetchCaseList()
        } catch (err) {
            ElMessage.error('保存失败: ' + err.message)
        } finally {
            loading.value = false
        }
    }

    async function runCase() {
        if (!currentCase.value.id) {
            ElMessage.warning('请先保存测试用例')
            return
        }

        running.value = true
        try {
            const res = await api.runTestCase(currentCase.value.id)
            if (res.data.success) {
                ElMessage.success('执行成功')
            } else {
                ElMessage.error('执行失败: 详见日志')
            }
        } catch (err) {
            ElMessage.error('执行出错: ' + err.message)
        } finally {
            running.value = false
        }
    }

    function addStep(step) {
        const normalized = isStandardStepPayload(step)
            ? standardStepToUiStep(step)
            : ensureCrossPlatformFields(step)
        normalized.description = ''
        currentCase.value.steps.push(normalized)
        lastAddedStepUuid.value = normalized.uuid || null
    }

    function updateStep(index, step) {
        currentCase.value.steps[index] = ensureCrossPlatformFields(step)
    }

    function removeStep(index) {
        currentCase.value.steps.splice(index, 1)
    }

    function newCase(opts = {}) {
        lastAddedStepUuid.value = null
        currentCase.value = {
            id: null,
            name: '未命名用例',
            variables: [],
            steps: [],
            folder_id: opts.folder_id || null
        }
        updateSnapshot()
    }

    return {
        // State
        currentCase,
        caseList,
        loading,
        running,
        lastAddedStepUuid,
        // Getters
        hasUnsavedChanges,
        // Actions
        fetchCaseList,
        loadCase,
        saveCase,
        runCase,
        addStep,
        updateStep,
        removeStep,
        newCase
    }
})
