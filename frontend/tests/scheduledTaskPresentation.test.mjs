import test from 'node:test'
import assert from 'node:assert/strict'

import { scheduledTaskExecution } from '../src/utils/scheduledTaskPresentation.js'

test('UI task includes an environment only when one is configured', () => {
  const withoutEnvironment = scheduledTaskExecution({ scenario_name: '登录回归', strategy_config: {} })
  assert.deepEqual(withoutEnvironment, {
    type: 'ui',
    typeLabel: 'UI 自动化',
    tagType: '',
    title: '登录回归',
    detail: '',
  })

  assert.equal(scheduledTaskExecution(
    { scenario_name: '登录回归', strategy_config: { env_id: 2 } },
    { environments: [{ id: 2, name: '预发布' }] },
  ).detail, '环境：预发布')
})

test('inspection task uses names and never leaks internal ids', () => {
  const result = scheduledTaskExecution({
    strategy_config: {
      _task_type: 'inspection',
      inspection_profile_id: 19,
      inspection_package_id: 31,
      inspection_branches: ['guest', 'authenticated'],
    },
  }, {
    inspectionProfiles: [{ id: 19, name: '商城巡检' }],
    packages: [{ id: 31, app_name: '智家', version_name: '8.2.0' }],
  })

  assert.deepEqual(result, {
    type: 'inspection',
    typeLabel: '智能巡检',
    tagType: 'success',
    title: '商城巡检',
    detail: '未登录、已登录 · 智家 8.2.0',
  })
  assert.doesNotMatch(`${result.title} ${result.detail}`, /19|31/)
})

test('fastbot task has one concise execution summary', () => {
  assert.deepEqual(scheduledTaskExecution({
    strategy_config: {
      _task_type: 'fastbot',
      fb_package_name: 'com.example.mall',
      fb_duration: 2700,
      fb_throttle: 500,
    },
  }), {
    type: 'fastbot',
    typeLabel: '智能探索',
    tagType: 'warning',
    title: 'com.example.mall',
    detail: '45 分钟 · 500 ms/次',
  })
})
