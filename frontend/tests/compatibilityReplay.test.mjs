import assert from 'node:assert/strict'
import test from 'node:test'

import {
  compatibilityStatusLabel,
  packageSnapshotLabel,
} from '../src/utils/compatibilityReplay.js'

test('labels compatibility run status in user language', () => {
  assert.equal(compatibilityStatusLabel('PASS'), '通过')
  assert.equal(compatibilityStatusLabel('warning'), '需关注')
  assert.equal(compatibilityStatusLabel('ABORTED'), '已中止')
  assert.equal(compatibilityStatusLabel(''), '等待执行')
  assert.equal(compatibilityStatusLabel('UNKNOWN_STATUS'), '状态未知')
})

test('renders package snapshots as name plus version', () => {
  assert.equal(packageSnapshotLabel({ package_name: 'com.example', version_name: '1.2.0' }), 'com.example 1.2.0')
  assert.equal(
    packageSnapshotLabel({ package_name: 'com.example', version_name: '1.2.0', version_code: 12 }),
    'com.example 1.2.0 (12)',
  )
  assert.equal(packageSnapshotLabel({ app_name: '商城' }), '商城')
  assert.equal(packageSnapshotLabel(null), '版本未知')
})
