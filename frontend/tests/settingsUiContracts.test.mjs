import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const source = await readFile(
  new URL('../src/views/settings/NotificationSettings.vue', import.meta.url),
  'utf8',
)

test('settings keeps one low-height scroll container and a reachable save bar', () => {
  assert.match(source, /\.notification-settings\s*\{[\s\S]*?flex:\s*1;[\s\S]*?height:\s*0;[\s\S]*?overflow-y:\s*auto;/)
  assert.equal((source.match(/overflow-y:\s*auto;/g) || []).length, 1)
  assert.match(source, /\.global-actions\s*\{[\s\S]*?position:\s*sticky;[\s\S]*?bottom:\s*0;/)
})

test('settings keeps retention tied to CAS and drops the retired inspection switches', () => {
  assert.match(source, /activeFeatureGroups\s*=\s*ref\(\[\]\)/)
  assert.match(
    source,
    /watch\(\(\)\s*=>\s*form\.value\.content_addressed_assets,[\s\S]*?form\.value\.tiered_asset_retention\s*=\s*false/,
  )
  assert.match(source, /:disabled="!form\.content_addressed_assets"/)
  assert.match(source, /需先启用报告资产去重/)
  // 智能巡检试验开关已随功能废弃整体移除
  assert.doesNotMatch(source, /model_inspection|inspection_/)
  assert.doesNotMatch(source, /巡检/)
})

test('settings blocks saving partial defaults and lazily loads capacity details', () => {
  assert.match(source, /loadError\.value\s*=/)
  assert.match(source, /:disabled="Boolean\(loadError\)"/)
  assert.match(source, /activeStorageGroups\.value\.includes\('capacity'\)/)
  assert.doesNotMatch(source, /form\.value\.tiered_asset_retention = false\s+await loadAssetStatus\(\)/)
})
