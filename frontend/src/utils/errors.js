// One place that turns any request failure into a sentence a tester can act
// on. Applied in the axios interceptor, so call sites that show
// `err.message` or `err.response.data.detail` get Chinese text without being
// touched; exported for new code that wants it directly.

const STATUS_TEXT = {
  400: '请求无效，请检查输入内容',
  401: '登录状态已失效，请重新登录',
  403: '没有权限执行此操作',
  404: '请求的资源不存在',
  405: '不支持的操作',
  408: '请求超时，请稍后重试',
  409: '操作冲突，请刷新后重试',
  413: '提交的内容过大',
  415: '不支持的内容格式',
  422: '提交的内容不符合要求',
  429: '操作过于频繁，请稍后再试',
  500: '服务内部错误，请稍后重试',
  502: '服务暂时不可用，请稍后重试',
  503: '服务暂时不可用，请稍后重试',
  504: '服务响应超时，请稍后重试',
}

// FastAPI's own validation errors: a list of {loc, msg, type}. Application
// errors are also lists, but carry `message` and never `loc`.
export const isValidationList = value => Array.isArray(value) && value.length > 0 && value.every(item => item && typeof item === 'object' && 'loc' in item && 'msg' in item)

export function describeDetail(detail) {
  if (typeof detail === 'string') return detail.trim()
  if (isValidationList(detail)) return detail.map(item => item.msg).join('；')
  if (Array.isArray(detail)) return detail.map(item => (item && (item.message || item.msg)) || (typeof item === 'string' ? item : JSON.stringify(item))).join('；')
  if (detail && typeof detail === 'object') {
    const related = detail.scenarios || detail.tasks
    const head = detail.message || detail.msg || ''
    return related?.length ? `${head}：${related.join('、')}` : head
  }
  return ''
}

export const isCanceled = error => !!error && (error.name === 'CanceledError' || error.code === 'ERR_CANCELED')

export function describeError(error) {
  if (!error) return '操作失败'
  if (isCanceled(error)) return ''
  const response = error.response
  if (!response) {
    if (error.code === 'ECONNABORTED' || /timeout/i.test(error.message || '')) return '请求超时，请检查网络后重试'
    if (error.request) return '无法连接服务器，请检查网络或服务状态'
    return error.message || '操作失败'
  }
  const data = response.data
  let text = ''
  if (data && typeof data === 'object' && !(typeof Blob !== 'undefined' && data instanceof Blob)) text = describeDetail(data.detail) || describeDetail(data.message) || ''
  else if (typeof data === 'string' && data && !/<\s*(!doctype|html)/i.test(data)) text = data.trim().slice(0, 200)
  return text || STATUS_TEXT[response.status] || `请求失败（HTTP ${response.status}）`
}

// Rewrites the error in place so every existing consumer sees Chinese text.
export function normalizeError(error) {
  if (!error || isCanceled(error)) return error
  const friendly = describeError(error)
  if (friendly) error.message = friendly
  const response = error.response
  if (!response) return error
  const data = response.data
  const isBlob = typeof Blob !== 'undefined' && data instanceof Blob
  if (data && typeof data === 'object' && !isBlob) {
    // Keep application structures (precheck lists, {message, scenarios});
    // only replace what is missing or is FastAPI's English validation list.
    if (data.detail === undefined || data.detail === null || isValidationList(data.detail)) data.detail = friendly
  } else if (!isBlob) {
    // Proxy/HTML error pages become the same structure every call site reads.
    response.data = { detail: friendly }
  }
  return error
}

/** 从 axios 错误对象中提取可读的错误信息（既有调用方保留此入口）。 */
export const getErrorDetail = (err, fallback = '操作失败') => describeError(err) || fallback
