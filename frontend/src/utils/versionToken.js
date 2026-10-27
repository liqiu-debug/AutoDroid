// 步骤写入乐观锁（P4 功能1）：PUT /api/cases/{id}/steps 与场景 steps 写入口
// 携带版本令牌，服务端校验失败返回 409（多端并发不再静默互相覆盖）。
//
// 契约以平台 backend/version_lock.py 为准（已实现并锁测试）：
//  - 令牌取值：实体上的整数 `version`（建档默认 1，每次步骤写入 +1；GET 响应携带）。
//  - 请求同时带 Header `If-Match: <version>`（不带引号）与 Query `version=<int>`；
//    两者都不带 → 服务端跳过校验（旧客户端兼容）。
//  - 非整数/失配令牌服务端一律返回 409（version_lock.py 无 400 路径），不会 400；
//    平台支持 updated_at ISO 令牌（If-Match / ?expected_updated_at=），但前端当前
//    只发整数令牌——ISO 令牌的格式/精度口径差异易触发伪 409，宁可暂不校验，
//    也不发伪令牌。
//  - 冲突返回 409，detail 固定文案（服务端 STEPS_VERSION_CONFLICT_DETAIL），
//    响应头 `X-Current-Version` 携带服务端当前 version。
//  - 校验通过后服务端递增 version 并在响应头 `X-Current-Version` 回传新令牌，
//    调用方必须回填缓存，否则下次保存拿旧令牌必然 409。

export const VERSION_CONFLICT_MESSAGE = '保存冲突：内容已被其他端修改，请重新拉取后再保存'

const hasText = (value) => value !== undefined && value !== null && String(value) !== ''
const isIntToken = (value) => /^\d+$/.test(String(value).trim())

/** 从响应体/实体里认领版本状态 {version, updated_at}；认不出返回 null。 */
export function versionStateFrom(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null
  if (hasText(payload.version) || hasText(payload.updated_at)) {
    return {
      version: hasText(payload.version) ? payload.version : null,
      updated_at: hasText(payload.updated_at) ? payload.updated_at : null,
    }
  }
  for (const key of ['data', 'case', 'scenario', 'entity']) {
    const nested = versionStateFrom(payload[key])
    if (nested) return nested
  }
  return null
}

/** 令牌优先 version（整数），回退 updated_at；无可用令牌返回 null。 */
export function versionTokenFrom(state) {
  if (!state || typeof state !== 'object') return null
  if (hasText(state.version)) return String(state.version)
  if (hasText(state.updated_at)) return String(state.updated_at)
  return null
}

const unquoteEtag = (value) => String(value).trim().replace(/^W\//i, '').replace(/^"|"$/g, '')

/** 保存成功响应（axios res）→ 新令牌状态，供回填；认不出返回 null。 */
export function extractVersionState(response) {
  const fromBody = versionStateFrom(response?.data)
  if (fromBody) return fromBody
  const headers = response?.headers || {}
  // 平台 version_lock.py 的回传头是 X-Current-Version（步骤写入成功/409 都带）。
  const current = headers['x-current-version'] ?? headers['X-Current-Version']
  const etag = unquoteEtag(headers.etag ?? headers.ETag ?? '')
  const xVersion = headers['x-version'] ?? headers['X-Version']
  const xUpdatedAt = headers['x-updated-at'] ?? headers['X-Updated-At']
  const state = {
    version: hasText(current) ? current : (hasText(xVersion) ? xVersion : (/^\d+$/.test(etag) ? etag : null)),
    updated_at: hasText(xUpdatedAt) ? xUpdatedAt : (etag && !/^\d+$/.test(etag) ? etag : null),
  }
  return state.version !== null || state.updated_at !== null ? state : null
}

/**
 * 拼 If-Match / ?version=；仅整数令牌才带（非整数/失配令牌平台返回 409 而非
 * 400，但发伪令牌只会换来伪 409 干扰保存；平台支持 updated_at ISO 令牌走
 * If-Match / ?expected_updated_at=，前端当前只发整数令牌是避免伪 409 的选择）；
 * 无令牌时返回空配置。
 */
export function versionRequestConfig(state) {
  const config = { headers: {}, params: {} }
  const token = versionTokenFrom(state)
  if (token === null || !isIntToken(token)) return config
  const version = String(Number(token))
  config.headers['If-Match'] = version
  config.params.version = version
  return config
}

/** 步骤保存 409：提示刷新重拉，不静默覆盖（草稿由调用方保留）。 */
export const isVersionConflict = (error) => !!error && error?.response?.status === 409
