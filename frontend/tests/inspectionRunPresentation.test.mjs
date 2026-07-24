import test from 'node:test'
import assert from 'node:assert/strict'

import {
  inspectionRunCoverage,
  inspectionRunReplay,
  inspectionRunStatusLabel,
} from '../src/utils/inspectionRunPresentation.js'

test('prefers page-family coverage counts when the new summary is available', () => {
  assert.deepEqual(
    inspectionRunCoverage({
      status: 'PASS',
      summary: {
        family_coverage: { representatives_expanded: 8, total: 10 },
      },
      total_states: 42,
    }),
    {
      label: '8/10',
      detail: '页面族代表 · 80%',
      percent: 80,
      source: 'family',
    },
  )
})

test('business journey coverage takes precedence over discovered family expansion', () => {
  assert.deepEqual(
    inspectionRunCoverage({
      status: 'WARNING',
      selected_branches: ['authenticated'],
      coverage_assessment: {
        assessment_origin: 'BACKFILLED_V1',
        selected_scope_verdict: 'COMPLETE',
        full_app_verdict: 'INCOMPLETE',
      },
      summary: {
        business_coverage: {
          covered_required: 11,
          total_required: 11,
          weighted_coverage: 0.965,
          selected_scope_verdict: 'COMPLETE',
          full_app_verdict: 'INCOMPLETE',
        },
        family_coverage: { representatives_expanded: 29, total: 43 },
      },
    }),
    {
      label: '11/11',
      detail: '核心旅程 · 96.5% · 已登录范围完整 · 全应用不完整',
      percent: 96.5,
      source: 'business',
    },
  )
})

test('run 47 remains partial when family expansion is high but core coverage is 10.5%', () => {
  assert.deepEqual(
    inspectionRunCoverage({
      status: 'WARNING',
      selected_branches: ['authenticated'],
      coverage_assessment: {
        selected_scope_verdict: 'PARTIAL',
        full_app_verdict: 'INCOMPLETE',
      },
      summary: {
        business_coverage: {
          covered_required: 2,
          total_required: 19,
          required_ratio: 0.105,
          selected_scope_verdict: 'PARTIAL',
          full_app_verdict: 'INCOMPLETE',
        },
        exploration_coverage: { expanded: 13, total: 14, ratio: 0.929 },
      },
    }),
    {
      label: '2/19',
      detail: '核心旅程 · 10.5% · 部分覆盖 · 全应用不完整',
      percent: 10.5,
      source: 'business',
    },
  )
})

test('supports graph v6 coverage and replay summary field names', () => {
  const run = {
    status: 'PASS',
    coverage: { family_coverage_rate: 0.825 },
    summary: {
      replay_eligible_count: 12,
      verified_path_count: 4,
      observed_replay_paths: 8,
    },
  }

  assert.equal(inspectionRunCoverage(run).label, '83%')
  assert.deepEqual(inspectionRunReplay(run), {
    label: '12 条',
    detail: '已复验 4 · 单次到达 8',
    source: 'replay',
    candidateCount: 12,
    defaultSelection: null,
  })
})

test('prefers graph v7 replay paths and exposes candidate selection semantics', () => {
  assert.deepEqual(inspectionRunReplay({
    status: 'PASS',
    summary: {
      replay_paths: {
        total: 117,
        full_path: 83,
        safety_prefix: 34,
        verified_twice: 12,
        observed_once: 105,
        candidate_count: 117,
        default_selection_limit: 20,
      },
    },
  }), {
    label: '117 条',
    detail: '兼容任务默认选择 20 条',
    source: 'replay',
    candidateCount: 117,
    defaultSelection: 20,
  })
})

test('does not count diagnostic candidates as replayable paths', () => {
  assert.deepEqual(inspectionRunReplay({
    status: 'PASS',
    summary: {
      replay_paths: {
        total: 115,
        full_path: 94,
        safe_prefix: 21,
        diagnostic_only: 2,
        candidate_count: 117,
      },
    },
  }), {
    label: '115 条',
    detail: '完整 94 · 安全前缀 21',
    source: 'replay',
    candidateCount: 115,
    defaultSelection: null,
  })
})

test('an explicit zero replay summary is rendered as zero instead of unknown', () => {
  assert.deepEqual(inspectionRunReplay({
    status: 'PASS',
    summary: {
      replay_paths: {
        total: 0,
        full_path: 0,
        safety_prefix: 0,
        verified_twice: 0,
        observed_once: 0,
      },
    },
  }), {
    label: '0 条',
    detail: '暂无可回放路径',
    source: 'replay',
    candidateCount: 0,
    defaultSelection: null,
  })
})

test('an ineligible source never presents captured evidence as replayable', () => {
  assert.deepEqual(inspectionRunReplay({
    status: 'ABORTED',
    replay_source_eligible: false,
    replay_source_reason: 'ABORTED',
    summary: {
      replay_paths: { total: 18, full_path: 12, safety_prefix: 6 },
    },
  }), {
    label: '0 条',
    detail: '任务已取消，已有采集证据仅供查看',
    source: 'ineligible',
    candidateCount: 0,
    defaultSelection: null,
  })
})

test('legacy runs expose only evidence actually present in the old list API', () => {
  const run = { status: 'PASS', total_states: 31, stable_count: 6 }
  assert.deepEqual(inspectionRunCoverage(run), {
    label: '31 页',
    detail: '已发现，待计算页面族覆盖',
    percent: null,
    source: 'legacy',
  })
  assert.deepEqual(inspectionRunReplay(run), {
    label: '6 条',
    detail: '旧报告已复验路径',
    source: 'legacy',
    candidateCount: 6,
    defaultSelection: null,
  })
})

test('zero legacy stable count is presented explicitly', () => {
  assert.deepEqual(inspectionRunReplay({ status: 'PASS', stable_count: 0 }), {
    label: '0 条',
    detail: '旧报告未记录可回放路径',
    source: 'legacy',
    candidateCount: 0,
    defaultSelection: null,
  })
  assert.deepEqual(inspectionRunReplay({ status: 'RUNNING', stable_count: 0 }), {
    label: '0 条',
    detail: '巡检中，暂未生成路径',
    source: 'legacy',
    candidateCount: 0,
    defaultSelection: null,
  })
})

test('run status labels use concise user-facing Chinese', () => {
  assert.equal(inspectionRunStatusLabel('RUNNING'), '巡检中')
  assert.equal(inspectionRunStatusLabel('WARNING'), '需关注')
  assert.equal(inspectionRunStatusLabel({ status: 'WARNING', stop_reason: '达到动作预算' }), '达到运行上限')
  assert.equal(inspectionRunStatusLabel({ status: 'WARNING', stop_reason: 'FRONTIER_INCOMPLETE' }), '探索未完整')
  assert.equal(inspectionRunStatusLabel('ABORTED'), '已取消')
})
