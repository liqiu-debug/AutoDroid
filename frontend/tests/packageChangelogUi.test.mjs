import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// 包管理「更新提示」（changelog）：桌面客户端启动自动检查更新时，弹窗正文就是这条。
// 契约：上传前可写（随上传会话提交）、列表可见、上传后仍可单独改（PATCH 不重传安装包）。
const page = readFileSync(new URL('../src/views/packages/PackageManagement.vue', import.meta.url), 'utf8')
const apiSource = readFileSync(new URL('../src/api/index.js', import.meta.url), 'utf8')

test('上传前填写更新提示，随分片上传会话一起提交', () => {
  assert.match(page, /const uploadChangelog = ref\(''\)/)
  assert.match(page, /v-model="uploadChangelog"/, '上传区有可编辑的更新提示输入框')
  assert.match(page, /更新提示（上传包时手写）|检查更新.*弹窗展示的文案/)
  assert.match(
    page,
    /changelog: uploadChangelog\.value\.trim\(\)/,
    '随 createPackageUploadSession 提交，服务端在 complete 时写进包记录',
  )
  assert.match(page, /uploadChangelog\.value = ''/, '上传成功后清空，避免下一版误带旧文案')
})

test('列表展示更新提示，并支持上传后单独补写/修改', () => {
  assert.match(page, /<el-table-column label="更新提示" min-width="200">/)
  assert.match(page, /<span v-if="row\.changelog" class="changelog-cell"/)
  assert.match(page, /<el-dropdown-item command="changelog">编辑更新提示<\/el-dropdown-item>/)
  assert.match(page, /const handleRowCommand = \(command, row\) => \{/)
  assert.match(page, /if \(command === 'changelog'\) return openChangelogDialog\(row\)/)
  assert.match(page, /const openChangelogDialog = \(row\) => \{[\s\S]*?changelogDraft\.value = row\?\.changelog \|\| ''/)
  assert.match(
    page,
    /api\.updatePackage\(target\.id, \{ changelog: changelogDraft\.value \}\)/,
    '只改文案，不必重传安装包',
  )
})

test('更新提示接口走 PATCH /packages/{id}', () => {
  assert.match(apiSource, /updatePackage\(id, data\) \{\s*\n\s*return api\.patch\(`\/packages\/\$\{id\}`, data\)/)
})
