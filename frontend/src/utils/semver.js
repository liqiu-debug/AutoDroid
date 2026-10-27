/**
 * semver 比较工具（桌面客户端「检查更新」用）。
 *
 * compareVersions(a, b) -> -1 | 0 | 1（a<b / a==b / a>b）：
 * - 容忍 'v'/'V' 前缀（v1.2.3 与 1.2.3 等价）；
 * - 容忍 x.y.z.w 四段及以上版本，按数值逐段比较，缺段补 0（1.2 与 1.2.0 等价）；
 * - 段按数值比较而非字典序（1.10.0 > 1.9.9）。
 */

function parseSegments(version) {
  const text = String(version ?? '').trim().replace(/^[vV]/, '')
  if (!text) return []
  return text.split('.').map(part => {
    const match = /^\d+/.exec(part.trim())
    return match ? Number.parseInt(match[0], 10) : 0
  })
}

/** 版本比较：返回 -1 | 0 | 1。仅当远端 > 本地（返回 1）时才提示更新。 */
export function compareVersions(a, b) {
  const left = parseSegments(a)
  const right = parseSegments(b)
  const length = Math.max(left.length, right.length)
  for (let i = 0; i < length; i += 1) {
    const l = left[i] ?? 0
    const r = right[i] ?? 0
    if (l < r) return -1
    if (l > r) return 1
  }
  return 0
}
