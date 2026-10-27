import assert from 'node:assert/strict'
import test from 'node:test'

import { compareVersions } from '../src/utils/semver.js'

// 桌面端「检查更新」版本比较契约：compareVersions(a, b) -> -1 | 0 | 1，
// 容忍 'v' 前缀与 x.y.z.w；仅当远端 > 本地（返回 1）才提示更新。
// 与仓库既有测试一致，node --test 直接导入纯工具模块（非 vitest）。

test('basic ordering: -1 / 0 / 1', () => {
  assert.equal(compareVersions('1.0.0', '1.0.1'), -1)
  assert.equal(compareVersions('1.0.1', '1.0.0'), 1)
  assert.equal(compareVersions('1.0.0', '1.0.0'), 0)
  assert.equal(compareVersions('2.0.0', '1.9.9'), 1)
})

test('segments compare numerically instead of lexicographically', () => {
  assert.equal(compareVersions('1.9.9', '1.10.0'), -1)
  assert.equal(compareVersions('1.10.0', '1.9.9'), 1)
  assert.equal(compareVersions('1.2.10', '1.2.9'), 1)
})

test("tolerates the 'v' prefix", () => {
  assert.equal(compareVersions('v1.2.3', '1.2.3'), 0)
  assert.equal(compareVersions('V1.2.3', 'v1.2.3'), 0)
  assert.equal(compareVersions('v1.2.3', '1.2.4'), -1)
  assert.equal(compareVersions('1.2.3', 'v1.2.2'), 1)
})

test('tolerates x.y.z.w four-segment versions', () => {
  assert.equal(compareVersions('1.2.3.4', '1.2.3'), 1)
  assert.equal(compareVersions('1.2.3', '1.2.3.4'), -1)
  assert.equal(compareVersions('1.2.3.0', '1.2.3'), 0)
  assert.equal(compareVersions('1.2.3.1', '1.2.3.2'), -1)
})

test('missing segments count as zero', () => {
  assert.equal(compareVersions('1.2', '1.2.0'), 0)
  assert.equal(compareVersions('1.2', '1.3'), -1)
  assert.equal(compareVersions('1', '1.0.0'), 0)
})

test('update gate: prompt only when remote is strictly newer than local', () => {
  assert.equal(compareVersions('1.0.0', '1.0.0'), 0, 'same version is not an update')
  assert.equal(compareVersions('0.9.9', '1.0.0'), -1, 'older remote is not an update')
  assert.equal(compareVersions('v1.0.1', '1.0.0'), 1, 'newer remote is the only prompt case')
})
