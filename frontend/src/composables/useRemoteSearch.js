import { ref, computed } from 'vue'

/**
 * el-select 远程搜索选择器的通用逻辑。
 *
 * 解决「选择器一次性全量拉取，超量静默截断」的问题：
 * - search(keyword)：调用注入的 fetcher 拉取一页候选（服务端 keyword 过滤）
 * - ensureOption(value)：编辑回显时，若当前候选中没有已选值，
 *   通过 resolver 单独拉取该记录并固定在候选列表头部，避免只显示裸 ID
 *
 * @param {Object} config
 * @param {(keyword: string) => Promise<Array>} config.fetcher 关键字搜索，返回候选数组
 * @param {(value: any) => Promise<Object|null>} [config.resolver] 按值兜底拉取单条记录
 * @param {string} [config.valueKey='id'] 候选对象中作为选中值的字段
 */
export function useRemoteSearch({ fetcher, resolver = null, valueKey = 'id' }) {
    const results = ref([])
    const pinned = ref([])
    const loading = ref(false)
    let requestSeq = 0

    const options = computed(() => {
        const seen = new Set()
        const merged = []
        for (const item of [...pinned.value, ...results.value]) {
            const key = item?.[valueKey]
            if (key === null || key === undefined || seen.has(key)) continue
            seen.add(key)
            merged.push(item)
        }
        return merged
    })

    const search = async (keyword = '') => {
        const seq = ++requestSeq
        loading.value = true
        try {
            const items = await fetcher(String(keyword ?? '').trim())
            if (seq === requestSeq) {
                results.value = Array.isArray(items) ? items : []
            }
        } catch (err) {
            console.error('远程搜索选项失败', err)
        } finally {
            if (seq === requestSeq) {
                loading.value = false
            }
        }
    }

    const ensureOption = async (value) => {
        if (value === null || value === undefined || value === '') return
        if (options.value.some(item => item?.[valueKey] === value)) return
        if (!resolver) return
        try {
            const item = await resolver(value)
            if (item && !pinned.value.some(p => p?.[valueKey] === item[valueKey])) {
                pinned.value = [item, ...pinned.value]
            }
        } catch (err) {
            console.error('拉取已选项失败', err)
        }
    }

    return { options, loading, search, ensureOption }
}
