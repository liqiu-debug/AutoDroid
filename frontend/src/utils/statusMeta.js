/**
 * 统一的状态元信息映射（el-tag type / 中文文案 / 主题色）。
 *
 * 覆盖两类状态：
 * - 执行/运行状态：用例与场景的 last_run_status、执行记录、兼容性任务、
 *   Fastbot/冷热启动任务（PASS/FAIL/ERROR/WARNING/RUNNING/PENDING/ABORTED/SKIP/COMPLETED）
 * - 设备状态：IDLE/BUSY/FASTBOT_RUNNING/OFFLINE/WDA_DOWN
 */

/** 归一化执行状态：统一大小写并合并同义词（success→PASS、failed→FAIL 等） */
export const normalizeRunStatus = (status) => {
    const normalized = String(status ?? '').trim().toUpperCase()
    if (!normalized) return ''
    if (normalized === 'SUCCESS') return 'PASS'
    if (normalized === 'FAILED' || normalized === 'FAILURE') return 'FAIL'
    if (normalized === 'CANCELLED') return 'ABORTED'
    if (normalized === 'RUNNING...') return 'RUNNING'
    return normalized
}

/** 执行状态 → el-tag type */
export const runStatusTagType = (status) => {
    const s = normalizeRunStatus(status)
    if (s === 'PASS' || s === 'COMPLETED') return 'success'
    if (s === 'WARNING' || s === 'QUEUED') return 'warning'
    if (s === 'FAIL' || s === 'ERROR') return 'danger'
    if (s === 'RUNNING') return 'primary'
    return 'info' // ABORTED / SKIP / PENDING / 未知
}

/** 执行状态 → 中文文案 */
export const runStatusLabel = (status) => {
    const s = normalizeRunStatus(status)
    const map = {
        PASS: '成功',
        FAIL: '失败',
        ERROR: '错误',
        WARNING: '告警',
        RUNNING: '运行中',
        PENDING: '排队中',
        QUEUED: '排队中',
        ABORTED: '已终止',
        SKIP: '跳过',
        COMPLETED: '已完成',
    }
    return map[s] || s
}

/** 执行状态 → 主题色（状态条等非 tag 场景） */
export const runStatusColor = (status) => {
    const s = normalizeRunStatus(status)
    if (s === 'PASS' || s === 'COMPLETED') return '#67C23A'
    if (s === 'WARNING' || s === 'ERROR' || s === 'QUEUED') return '#E6A23C'
    if (s === 'FAIL') return '#F56C6C'
    if (s === 'RUNNING') return '#409EFF'
    return '#909399' // ABORTED / PENDING / 未执行 / 未知
}

const DEVICE_STATUS_META = {
    IDLE: { tagType: 'success', label: '🟢 空闲' },
    BUSY: { tagType: 'danger', label: '🔴 执行中' },
    FASTBOT_RUNNING: { tagType: 'danger', label: '🔴 跑测中' },
    OFFLINE: { tagType: 'info', label: '⚫ 离线' },
    WDA_DOWN: { tagType: 'warning', label: '🟠 WDA异常' },
}

/** 设备状态 → el-tag type */
export const deviceStatusTagType = (status) => DEVICE_STATUS_META[status]?.tagType || 'info'

/** 设备状态 → 中文标签 */
export const deviceStatusLabel = (status) => DEVICE_STATUS_META[status]?.label || status
