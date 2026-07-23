const asFiniteNumber = value => {
  if (value === null || value === undefined || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

const firstNumber = (...values) => {
  for (const value of values) {
    const number = asFiniteNumber(value)
    if (number !== null) return number
  }
  return null
}

const nested = (value, key) => (
  value && typeof value === 'object' && !Array.isArray(value) ? value[key] : undefined
)

const isActive = status => ['PENDING', 'QUEUED', 'RUNNING'].includes(String(status || '').toUpperCase())

const replaySourceReasonLabel = reason => {
  const value = String(reason || '').trim()
  const normalized = value.toUpperCase()
  return {
    ABORTED: '任务已取消，已有采集证据仅供查看',
    CANCELLED: '任务已取消，已有采集证据仅供查看',
    PATH_DIVERGED: '回放路径不稳定，已有证据仅供查看',
    UNSTABLE: '回放路径不稳定，已有证据仅供查看',
    BUDGET_LIMIT: '任务达到预算上限，暂不用于兼容回放',
    FRONTIER_INCOMPLETE: '页面探索未完整收敛，暂不用于兼容回放',
  }[normalized] || value || '当前任务暂不用于兼容回放'
}

export const inspectionRunStatusLabel = value => {
  const run = value && typeof value === 'object' ? value : { status: value }
  const status = String(run.status || 'PENDING').toUpperCase()
  const stopReason = String(run.stop_reason || run.termination_reason || '')
  if (status === 'WARNING' && /预算|运行上限|时间上限|动作上限/.test(stopReason)) {
    return '达到运行上限'
  }
  if (status === 'WARNING' && /FRONTIER_INCOMPLETE|前沿仍未耗尽|探索未完整/.test(stopReason)) {
    return '探索未完整'
  }
  return ({
  PENDING: '待执行',
  QUEUED: '排队中',
  RUNNING: '巡检中',
  PASS: '已完成',
  WARNING: '需关注',
  FAIL: '失败',
  ERROR: '失败',
  ABORTED: '已取消',
  CANCELLED: '已取消',
  }[status] || status)
}

export const inspectionRunCoverage = run => {
  const summary = run?.summary || {}
  const coverage = run?.coverage || summary.coverage || summary.family_coverage || {}
  const stats = run?.stats || {}
  const total = firstNumber(
    coverage.total,
    coverage.families,
    coverage.discovered,
    summary.families_discovered,
    summary.family_count,
    stats.families_discovered,
    stats.families,
    run?.families_discovered,
    run?.family_count,
  )
  const expanded = firstNumber(
    coverage.expanded,
    coverage.representatives_expanded,
    summary.family_representatives_expanded,
    stats.family_representatives_expanded,
    run?.family_representatives_expanded,
  )
  const ratio = firstNumber(
    coverage.ratio,
    coverage.family_coverage_ratio,
    coverage.family_coverage_rate,
    summary.family_coverage_ratio,
    summary.family_coverage_rate,
    stats.family_coverage_ratio,
    run?.family_coverage_ratio,
  )

  if (total !== null && expanded !== null) {
    const percent = total > 0 ? Math.round((expanded / total) * 100) : 0
    return {
      label: `${Math.max(0, expanded)}/${Math.max(0, total)}`,
      detail: `页面族代表 · ${percent}%`,
      percent,
      source: 'family',
    }
  }
  if (ratio !== null) {
    const percent = Math.round(Math.max(0, Math.min(1, ratio)) * 100)
    return { label: `${percent}%`, detail: '页面族代表', percent, source: 'family' }
  }

  const discoveredPages = firstNumber(
    summary.reached_pages,
    summary.observed_states,
    run?.reached_page_count,
    run?.total_states,
  )
  if (discoveredPages !== null && discoveredPages > 0) {
    return {
      label: `${discoveredPages} 页`,
      detail: '已发现，待计算页面族覆盖',
      percent: null,
      source: 'legacy',
    }
  }
  return {
    label: isActive(run?.status) ? '发现中' : '暂无页面',
    detail: isActive(run?.status) ? '正在探索' : '未采集到页面',
    percent: null,
    source: 'empty',
  }
}

export const inspectionRunReplay = run => {
  const summary = run?.summary || {}
  const replay = run?.replay || summary.replay || summary.replay_paths || {}
  const candidateCount = firstNumber(
    nested(replay, 'candidate_count'),
    nested(replay, 'candidates'),
    summary.replay_candidate_count,
    run?.replay_candidate_count,
  )
  const defaultSelection = firstNumber(
    nested(replay, 'default_selection_limit'),
    nested(replay, 'default_selected'),
    nested(replay, 'selected'),
    summary.default_selection_limit,
    summary.default_selected_replay_count,
    run?.default_selection_limit,
    run?.default_selected_replay_count,
  )
  const verified = firstNumber(
    nested(replay, 'verified'),
    nested(replay, 'verified_twice'),
    summary.verified_replay_paths,
    summary.verified_path_count,
    run?.verified_replay_path_count,
  )
  const observed = firstNumber(
    nested(replay, 'observed'),
    nested(replay, 'observed_once'),
    summary.observed_replay_paths,
    run?.observed_replay_path_count,
  )
  const full = firstNumber(
    nested(replay, 'full'),
    nested(replay, 'full_path'),
    summary.full_replay_paths,
    run?.full_replay_path_count,
  )
  const safePrefix = firstNumber(
    nested(replay, 'safe_prefix'),
    nested(replay, 'safe_prefixes'),
    nested(replay, 'safety_prefix'),
    summary.safe_prefix_paths,
    run?.safe_prefix_path_count,
  )
  const replayTotal = firstNumber(
    nested(replay, 'total'),
    nested(replay, 'available'),
    nested(replay, 'replayable_count'),
    summary.replay_eligible_count,
    summary.replay_path_count,
    summary.safe_replay_path_count,
    run?.replay_path_count,
    run?.safe_replay_path_count,
  )
  const total = firstNumber(
    replayTotal,
    full !== null || safePrefix !== null ? (full || 0) + (safePrefix || 0) : null,
    verified !== null || observed !== null ? (verified || 0) + (observed || 0) : null,
    candidateCount,
  )

  if (run?.replay_source_eligible === false) {
    return {
      label: '0 条',
      detail: replaySourceReasonLabel(run?.replay_source_reason),
      source: 'ineligible',
      candidateCount: 0,
      defaultSelection: null,
    }
  }

  if (total !== null) {
    let detail = '可安全回放'
    if (defaultSelection !== null) {
      detail = `兼容任务默认选择 ${Math.max(0, Math.min(total, defaultSelection))} 条`
    } else if (verified !== null || observed !== null) {
      detail = `已复验 ${verified || 0} · 单次到达 ${observed || 0}`
    } else if (full !== null || safePrefix !== null) {
      detail = `完整 ${full || 0} · 安全前缀 ${safePrefix || 0}`
    }
    return {
      label: `${Math.max(0, total)} 条`,
      detail: total > 0 ? detail : '暂无可回放路径',
      source: replayTotal !== null || full !== null || safePrefix !== null ? 'replay' : 'candidate',
      candidateCount: Math.max(0, total),
      defaultSelection: defaultSelection === null
        ? null
        : Math.max(0, Math.min(total, defaultSelection)),
    }
  }

  const legacyVerified = firstNumber(run?.stable_count)
  if (legacyVerified !== null) {
    return {
      label: `${Math.max(0, legacyVerified)} 条`,
      detail: isActive(run?.status)
        ? '巡检中，暂未生成路径'
        : (legacyVerified > 0 ? '旧报告已复验路径' : '旧报告未记录可回放路径'),
      source: 'legacy',
      candidateCount: Math.max(0, legacyVerified),
      defaultSelection: null,
    }
  }
  return {
    label: isActive(run?.status) ? '生成中' : '报告内查看',
    detail: isActive(run?.status) ? '随探索持续生成' : '旧报告未汇总安全前缀',
    source: 'unknown',
    candidateCount: null,
    defaultSelection: null,
  }
}
