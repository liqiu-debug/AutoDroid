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
