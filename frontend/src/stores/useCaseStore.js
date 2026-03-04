import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '@/api'
import { ElMessage } from 'element-plus'

export const useCaseStore = defineStore('case', () => {
    // State
    const currentCase = ref({
        id: null,
        name: 'New Test Case',
        variables: [],
        steps: []
    })

    const caseList = ref([])
    const loading = ref(false)
    const running = ref(false)

    // Getters
    const hasUnsavedChanges = computed(() => {
        return currentCase.value.id === null
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
            const res = await api.getTestCase(id)
            // Ensure every step has a UI-only unique ID for drag-and-drop
            if (res.data.steps) {
                res.data.steps = res.data.steps.map(s => ({
                    ...s,
                    uuid: s.uuid || crypto.randomUUID(),
                    error_strategy: s.error_strategy || 'ABORT'
                }))
            }
            currentCase.value = res.data
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
            // Strip uuids before saving if backend doesn't want them? 
            // Or just keep them, they are harmless JSON fields.
            // Let's keep them for now, assuming permissive schema.
            let res
            if (currentCase.value.id) {
                // Update existing case
                res = await api.updateTestCase(currentCase.value.id, currentCase.value)
            } else {
                // Create new case
                res = await api.createTestCase(currentCase.value)
            }
            currentCase.value = res.data
            ElMessage.success('保存成功')
            // Refresh list
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
            console.log(res.data)
        } catch (err) {
            ElMessage.error('执行出错: ' + err.message)
        } finally {
            running.value = false
        }
    }

    function addStep(step) {
        if (!step.uuid) {
            step.uuid = crypto.randomUUID()
        }
        if (!step.error_strategy) {
            step.error_strategy = 'ABORT'
        }
        currentCase.value.steps.push(step)
    }

    function updateStep(index, step) {
        currentCase.value.steps[index] = step
    }

    function removeStep(index) {
        currentCase.value.steps.splice(index, 1)
    }

    function newCase(opts = {}) {
        currentCase.value = {
            id: null,
            name: 'New Test Case',
            variables: [],
            steps: [],
            folder_id: opts.folder_id || null
        }
    }

    return {
        // State
        currentCase,
        caseList,
        loading,
        running,
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
