import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// 包管理上传/下载与平台 SUPPORTED_PACKAGE_EXTENSIONS 同步（桌面通道 .exe/.msi/.dmg）：
// 上传白名单与错误文案、桌面包下载扩展映射、桌面行不显示「安装到设备」。
const source = readFileSync(new URL('../src/views/packages/PackageManagement.vue', import.meta.url), 'utf8')

test('upload whitelist accepts the desktop installers and keeps the platform error copy', () => {
  assert.match(source, /accept="\.apk,\.ipa,\.exe,\.msi,\.dmg"/)
  assert.match(source, /\['apk', 'ipa', 'exe', 'msi', 'dmg'\]\.includes\(extension\)/)
  assert.match(source, /仅支持 \.apk、\.ipa、\.exe、\.msi 或 \.dmg 文件/, '与平台 400 文案同步')
})

test('desktop rows download with the real installer extension and hide the device install entry', () => {
  assert.match(source, /const isDesktopPackage = \(row\) => \['windows', 'macos'\]/)
  assert.match(source, /v-else-if="!isDesktopPackage\(row\)"/, '桌面包无对应设备，不显示安装入口')
  assert.match(source, /link\.download = `\$\{row\.app_name\}_\$\{row\.version_name\}\.\$\{packageExtension\(row\)\}`/)
  assert.match(source, /\{ ios: 'ipa', windows: 'exe', macos: 'dmg' \}/)
})
