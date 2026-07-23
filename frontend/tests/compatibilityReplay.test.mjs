import assert from 'node:assert/strict'
import test from 'node:test'

import {
  boundaryEvidenceLabel,
  compatibilityExecutionMode,
  normalizeReplayPreflight,
  normalizeReplayResults,
  normalizeReplayScope,
  normalizeReplayTrace,
  packageSnapshotLabel,
  replayBoundaryEvidenceLabel,
  replayPathLabel,
  replayRoleLabel,
  replayScopeLabel,
  resolveInspectionRunRouteSelection,
  sourceBoundaryEvidenceLabel,
  terminalOutcomeLabel,
} from '../src/utils/compatibilityReplay.js'

test('normalizes replay preflight chains and keeps version notices as warnings', () => {
  const result = normalizeReplayPreflight({
    source: { package_name: 'com.example', known: false },
    installed_package: { package_name: 'com.example', version_name: '1.0.0' },
    blockers: [
      { code: 'SAME_VERSION', message: '当前版本相同' },
      { code: 'TARGET_VERSION_UNKNOWN', message: '无法读取设备 versionCode' },
      { code: 'UNSAFE_ENTRY_CASE', message: '入口用例包含风险动作' },
    ],
    chains: [{
      id: 7,
      title: '首页到订单',
      evidence_grade: 'VERIFIED_TWICE',
      replay_eligibility: 'SAFE_PREFIX',
      terminal_boundaries: [{ terminal_outcome: 'SAFETY_BLOCKED' }],
    }],
    plan_digest: 'digest-1',
  })

  assert.equal(result.blockers.length, 1)
  assert.equal(result.blockers[0].code, 'UNSAFE_ENTRY_CASE')
  assert.equal(result.warnings.length, 2)
  assert.equal(result.chains[0].chain_id, '7')
  assert.equal(result.chains[0].name, '首页到订单')
  assert.equal(result.chains[0].replay_scope, 'PREFIX_TO_SAFETY_BOUNDARY')
  assert.equal(result.chains[0].terminal_outcome, 'SAFETY_BLOCKED')
  assert.equal(result.plan_digest, 'digest-1')
  assert.equal(packageSnapshotLabel(result.source_package), 'com.example')
})

test('presents replay scope and terminal evidence in user language', () => {
  assert.equal(normalizeReplayScope('SAFE_PREFIX'), 'PREFIX_TO_SAFETY_BOUNDARY')
  assert.equal(replayScopeLabel('PREFIX_TO_SAFETY_BOUNDARY'), '回放到安全边界')
  assert.equal(terminalOutcomeLabel('LOCATOR_FAILED'), '控件定位失败')
  assert.equal(boundaryEvidenceLabel('NOT_VERIFIABLE'), '边界无法可靠确认')
  assert.equal(sourceBoundaryEvidenceLabel('VERIFIED'), '源报告已确认')
  assert.equal(replayBoundaryEvidenceLabel('CHANGED'), '升级后边界已变化')
})

test('resolves an explicit inspection report by id without falling back to the recent list', () => {
  const resolved = resolveInspectionRunRouteSelection({
    routeValue: '19',
    recentRuns: [{ id: 520, name: '最新报告' }],
    routeRun: { id: 19, name: '指定的历史报告' },
  })
  assert.equal(resolved.explicit, true)
  assert.equal(resolved.selectionId, 19)
  assert.deepEqual(resolved.options.map(item => item.id), [19, 520])
  assert.equal(resolved.blocker, '')

  const failed = resolveInspectionRunRouteSelection({
    routeValue: '19',
    recentRuns: [{ id: 520, name: '最新报告' }],
    loadError: '报告不存在',
  })
  assert.equal(failed.selectionId, '')
  assert.equal(failed.options[0].id, 520)
  assert.match(failed.blocker, /报告不存在/)

  const invalid = resolveInspectionRunRouteSelection({
    routeValue: 'latest',
    recentRuns: [{ id: 520, name: '最新报告' }],
  })
  assert.equal(invalid.selectionId, '')
  assert.match(invalid.blocker, /编号无效/)
})

test('flattens replay page results from a compatibility cell and aliases assets', () => {
  const results = normalizeReplayResults({
    execution_mode: 'installed_replay',
    page_set_snapshot: [{
      chain_id: 'chain-home',
      path_key: 'home>detail',
      name: '首页到详情',
      checkpoints: [{ role: 'HOME' }, { role: 'PRODUCT_DETAIL' }],
      covered_roles: ['HOME', 'PRODUCT_DETAIL'],
    }],
    cells: [{
      id: 3,
      device_serial: 'device-1',
      pages: [{
        id: 9,
        path_key: 'home>detail',
        screenshot_asset_id: 'shot-1',
        xml_asset_id: 'xml-1',
        replay_trace: [{ action_role: 'NAV', status: 'PASS' }],
        metrics: {
          duration_ms: 321,
          checkpoint_count: 2,
          replay_scope: 'FULL_PATH',
          terminal_outcome: 'NONE',
        },
      }],
    }],
  })

  assert.equal(results.length, 1)
  assert.equal(results[0].chain_id, 'chain-home')
  assert.equal(results[0].path_key, 'home>detail')
  assert.equal(results[0].candidate_screenshot_asset_id, 'shot-1')
  assert.equal(results[0].candidate_xml_asset_id, 'xml-1')
  assert.equal(results[0].device_serial, 'device-1')
  assert.equal(results[0].name, '首页到详情')
  assert.deepEqual(results[0].checkpoints.map(item => item.role), ['HOME', 'PRODUCT_DETAIL'])
  assert.deepEqual(results[0].covered_roles, ['HOME', 'PRODUCT_DETAIL'])
  assert.equal(results[0].duration_ms, 321)
  assert.equal(results[0].checkpoint_count, 2)
  assert.equal(results[0].replay_scope, 'FULL_PATH')
  assert.equal(results[0].terminal_outcome, 'NONE')
  assert.equal(compatibilityExecutionMode({ execution_mode: 'INSTALLED_REPLAY' }), 'installed_replay')
})

test('normalizes replay trace without exposing input values', () => {
  const trace = normalizeReplayTrace(JSON.stringify({ steps: [{
    step_index: 2,
    action_role_key: 'ITEM_OPEN',
    expected_page_role: 'LIST',
    actual_page_role: 'PRODUCT_DETAIL',
    duration_ms: 125,
    status: 'PASS',
  }] }))

  assert.equal(trace.length, 1)
  assert.equal(trace[0].index, 2)
  assert.equal(trace[0].name, '打开内容')
  assert.equal(trace[0].raw_name, 'ITEM_OPEN')
  assert.equal(trace[0].status_label, '通过')
  assert.equal(trace[0].expected_role, 'LIST')
  assert.equal(trace[0].actual_role, 'PRODUCT_DETAIL')
  assert.equal(trace[0].expected_role_label, '列表页')
  assert.equal(trace[0].actual_role_label, '商品详情')
  assert.equal(trace[0].duration_ms, 125)
})

test('freezes user-facing page and capture references while keeping internal ids separate', () => {
  const results = normalizeReplayResults({
    execution_mode: 'installed_replay',
    page_set_snapshot: [{
      chain_id: 'chain-product',
      path_key: 'opaque-path-key',
      name: 'PRODUCT_DETAIL (S1390)',
      display_label: 'P02',
      endpoint_state_id: 1390,
      source_observation_id: 220,
      boundary_evidence: 'VERIFIED',
      terminal_outcome: 'SAFETY_BLOCKED',
      replay_scope: 'PREFIX_TO_SAFETY_BOUNDARY',
      checkpoints: [
        { state_id: 1388, source_observation_id: 210, display_label: 'P01', role: 'HOME' },
        { state_id: 1390, source_observation_id: 220, display_label: 'P02', role: 'PRODUCT_DETAIL' },
      ],
    }],
    cells: [{
      id: 1,
      pages: [{
        id: 7,
        page_key: 'chain-product',
        status: 'WARNING',
        replay_trace: [{
          action_role: 'BOUNDARY_PROBE',
          status: 'BLOCKED',
          boundary_evidence: 'CHANGED',
          source: { actual: { role: 'CHECKOUT' } },
          target: { actual: { role: 'PAYMENT' } },
        }],
        metrics: {
          replay_scope: 'PREFIX_TO_SAFETY_BOUNDARY',
          terminal_outcome: 'SAFETY_BLOCKED',
        },
      }],
    }],
  })

  assert.equal(results[0].name, '商品详情')
  assert.equal(results[0].source_reference, 'P002 · 商品详情')
  assert.equal(results[0].source_capture_label, '第 1 次采集')
  assert.equal(results[0].source_boundary_evidence, 'VERIFIED')
  assert.equal(results[0].replay_boundary_evidence, 'CHANGED')
  assert.equal(results[0].replay_trace[0].expected_role_label, '结算页')
  assert.equal(results[0].replay_trace[0].actual_role_label, '支付页')
  assert.equal(replayPathLabel(results[0]), 'P001 · 首页 → P002 · 商品详情')
  assert.equal(results[0].source_state_id, 1390)
  assert.equal(results[0].source_observation_id, 220)
})

test('translates known page-role names instead of exposing raw enums', () => {
  const [result] = normalizeReplayResults({
    execution_mode: 'installed_replay',
    page_set_snapshot: [{
      chain_id: 'chain-search',
      name: 'SEARCH',
      endpoint_state_id: 2,
      checkpoints: [
        { state_id: 1, role: 'HOME' },
        { state_id: 2, role: 'SEARCH' },
      ],
    }],
    cells: [{ pages: [{ page_key: 'chain-search', status: 'PASS' }] }],
  })

  assert.equal(result.name, '搜索页')
  assert.equal(result.source_reference, 'P002 · 搜索页')
  assert.equal(replayRoleLabel('MODAL_PANEL'), '弹窗')
  assert.equal(replayRoleLabel('APPOINTMENT_LIST'), '预约列表')
})

test('never presents source boundary evidence as an upgrade execution result', () => {
  const [result] = normalizeReplayResults({
    execution_mode: 'installed_replay',
    page_set_snapshot: [{
      chain_id: 'chain-checkout',
      path_key: 'checkout-path',
      name: 'CHECKOUT',
      endpoint_state_id: 8,
      boundary_evidence: 'VERIFIED',
      terminal_outcome: 'SAFETY_BLOCKED',
      replay_scope: 'PREFIX_TO_SAFETY_BOUNDARY',
      checkpoints: [{ state_id: 8, role: 'CHECKOUT' }],
    }],
    cells: [{
      pages: [{
        page_key: 'chain-checkout',
        status: 'WARNING',
        replay_trace: [],
      }],
    }],
  })

  assert.equal(result.source_boundary_evidence, 'VERIFIED')
  assert.equal(result.replay_boundary_evidence, 'UNKNOWN')
})
