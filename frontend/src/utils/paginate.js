/**
 * 分页拉全量工具。
 *
 * 背景：列表接口都是「skip + limit + total」的分页契约，而调用点习惯写
 * `limit: 1000` 一次性取——超过 1000 条就会**静默截断**：列表少了东西、
 * 树上看得见的节点点了没反应（查不到详情）、关联对象显示「未知用例」。
 * 这里按服务端 total 循环取完，避免任何一处再埋同类雷。
 */

/**
 * @param {(page: {skip: number, limit: number}) => Promise<{total?: number, items?: any[]} | any[]>} fetchPage
 * @param {{pageSize?: number}} [options]
 * @returns {Promise<any[]>}
 */
export async function fetchAllPages(fetchPage, { pageSize = 500 } = {}) {
    const all = []
    let skip = 0
    for (;;) {
        const body = await fetchPage({ skip, limit: pageSize })
        const page = Array.isArray(body) ? body : (body?.items || [])
        all.push(...page)
        const total = Array.isArray(body) ? NaN : Number(body?.total)
        if (page.length === 0) break
        // 优先按服务端 total 判定拉完；缺失 total 时按「整页未满」收尾
        if (Number.isFinite(total) ? all.length >= total : page.length < pageSize) break
        skip += page.length
    }
    return all
}
