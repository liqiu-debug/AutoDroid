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
    if (s === 'PASS' || s === 'COMPLETED') return 'var(--ad-success)'
    if (s === 'WARNING' || s === 'ERROR' || s === 'QUEUED') return 'var(--ad-warning)'
    if (s === 'FAIL') return 'var(--ad-danger)'
    if (s === 'RUNNING') return 'var(--ad-primary)'
    return 'var(--ad-muted)' // ABORTED / PENDING / 未执行 / 未知
}

const DEVICE_STATUS_META = {
    IDLE: { tagType: 'success', label: '空闲' },
    BUSY: { tagType: 'danger', label: '执行中' },
    FASTBOT_RUNNING: { tagType: 'danger', label: '跑测中' },
    OFFLINE: { tagType: 'info', label: '离线' },
    WDA_DOWN: { tagType: 'warning', label: 'WDA异常' },
}

/** 设备状态 → el-tag type */
export const deviceStatusTagType = (status) => DEVICE_STATUS_META[status]?.tagType || 'info'

/** 设备状态 → 中文标签 */
export const deviceStatusLabel = (status) => DEVICE_STATUS_META[status]?.label || status

/**
 * 执行方式标签：App（设备端 App 直跑/领取任务）| 平台（平台控机执行）。
 * 兼容未回填 run_source 的历史数据（旧 batch_name 为固定设备端批次名）。
 */
export const runSourceLabel = (execution) => {
    const source = String(execution?.run_source || '').trim().toLowerCase()
    if (source === 'app') return 'App 执行'
    if (source === 'platform') return '平台执行'
    const batchName = String(execution?.batch_name || '')
    if (batchName === 'App 设备端执行' || batchName === 'AutoDroid 设备端执行' || batchName === 'AutoJs6 设备端执行') return 'App 执行'
    return '—'
}

/**
 * 执行对比 diff 分类元信息（延伸自执行状态映射）：
 * regressed 新失败(红) / fixed 已修复(绿) / still-failing 持续失败(灰红) /
 * unchanged 无变化(灰) / added 新增步骤 / removed 移除步骤
 */
const DIFF_CHANGE_META = {
    regressed: { tagType: 'danger', effect: 'dark', label: '新失败', rowClass: 'diff-row-regressed' },
    fixed: { tagType: 'success', effect: 'dark', label: '已修复', rowClass: 'diff-row-fixed' },
    'still-failing': { tagType: 'danger', effect: 'plain', label: '持续失败', rowClass: 'diff-row-still-failing' },
    unchanged: { tagType: 'info', effect: 'plain', label: '无变化', rowClass: '' },
    added: { tagType: 'primary', effect: 'plain', label: '新增步骤', rowClass: 'diff-row-added' },
    removed: { tagType: 'info', effect: 'dark', label: '移除步骤', rowClass: 'diff-row-removed' },
}

/** diff 分类 → el-tag type */
export const diffChangeTagType = (change) => DIFF_CHANGE_META[change]?.tagType || 'info'

/** diff 分类 → el-tag effect */
export const diffChangeEffect = (change) => DIFF_CHANGE_META[change]?.effect || 'plain'

/** diff 分类 → 中文标签 */
export const diffChangeLabel = (change) => DIFF_CHANGE_META[change]?.label || change

/** diff 分类 → 表格行 class */
export const diffChangeRowClass = (change) => DIFF_CHANGE_META[change]?.rowClass || ''
