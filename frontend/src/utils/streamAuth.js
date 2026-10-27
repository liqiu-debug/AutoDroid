/**
 * streamAuth - 浏览器侧 WS / 媒体流 URL 的构建与查询令牌拼接。
 *
 * 云端直连（鉴权收口）后 `/ws/scrcpy/{serial}`、`/ws/ios-mjpeg/{serial}`、
 * `GET /api/stream/ios-mjpeg/{serial}` 等通道要求携带 token：浏览器 WebSocket
 * 无法携带 Authorization 头，平台侧对这些端点支持 `?token=` 查询通道
 * （WS 握手 `_extract_ws_token`、REST `get_current_user_allow_query_token`）。
 * 桌面引擎本地端点忽略 query，多带一个参数无害，因此两种模式共用同一拼接。
 */

/** 读取当前登录令牌（缺失时返回空串，调用方按无 token 原样放行）。 */
export function readStreamToken(storage) {
  const store = storage ?? (typeof window !== 'undefined' ? window.localStorage : null)
  try {
    return String(store?.getItem('token') || '')
  } catch (err) {
    return ''
  }
}

/**
 * 在 URL 上写入 `token` 查询参数（已存在时覆盖，重连/重登后必须换新票）。
 * 纯函数：token 为空或 URL 为空时原样返回。
 */
export function withStreamToken(rawUrl, token) {
  const url = String(rawUrl ?? '')
  const value = String(token ?? '').trim()
  if (!url || !value) return url
  const hashIndex = url.indexOf('#')
  const base = hashIndex === -1 ? url : url.slice(0, hashIndex)
  const hash = hashIndex === -1 ? '' : url.slice(hashIndex)
  const queryIndex = base.indexOf('?')
  const path = queryIndex === -1 ? base : base.slice(0, queryIndex)
  const params = new URLSearchParams(queryIndex === -1 ? '' : base.slice(queryIndex + 1))
  params.set('token', value)
  return `${path}?${params.toString()}${hash}`
}

/** withStreamToken + readStreamToken 的组合：给 WS / <img> 媒体 URL 统一收口。 */
export function withCurrentStreamToken(rawUrl, storage) {
  return withStreamToken(rawUrl, readStreamToken(storage))
}

function streamLocation(locationLike) {
  return locationLike
    ?? (typeof window !== 'undefined' ? window.location : { protocol: 'http:', host: 'localhost' })
}

/** Android 投屏：`{ws|wss}://{host}/ws/scrcpy/{serial}?token=`（云端 4401 防护）。 */
export function buildScrcpyWsUrl(serial, locationLike, storage) {
  const loc = streamLocation(locationLike)
  const protocol = loc.protocol === 'https:' ? 'wss:' : 'ws:'
  return withCurrentStreamToken(
    `${protocol}//${loc.host}/ws/scrcpy/${encodeURIComponent(String(serial || ''))}`,
    storage
  )
}

/** iOS 实时画面：`{ws|wss}://{host}/ws/ios-mjpeg/{serial}?token=`。 */
export function buildIosMjpegWsUrl(serial, locationLike, storage) {
  const loc = streamLocation(locationLike)
  const protocol = loc.protocol === 'https:' ? 'wss:' : 'ws:'
  return withCurrentStreamToken(
    `${protocol}//${loc.host}/ws/ios-mjpeg/${encodeURIComponent(String(serial || ''))}`,
    storage
  )
}

/** iOS MJPEG REST 兜底（multipart，可直接 `<img src>`）：同样要求 ?token=。 */
export function buildIosMjpegImgUrl(serial, locationLike, storage) {
  const loc = streamLocation(locationLike)
  const origin = loc.origin || `${loc.protocol === 'https:' ? 'https:' : 'http:'}//${loc.host}`
  return withCurrentStreamToken(
    `${origin}/api/stream/ios-mjpeg/${encodeURIComponent(String(serial || ''))}`,
    storage
  )
}
