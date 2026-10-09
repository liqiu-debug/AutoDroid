import { createUuid } from './uuid.js'
import { describeError } from './errors.js'

export const copy = value => JSON.parse(JSON.stringify(value))
export const literal = (value = '') => ({ kind: 'literal', value })
export function fromJson(value) {
  if (Array.isArray(value)) return { kind: 'array', items: value.map(fromJson) }
  if (value !== null && typeof value === 'object') return { kind: 'object', fields: Object.fromEntries(Object.entries(value).map(([k, v]) => [k, fromJson(v)])) }
  return literal(value)
}
// A bound value cannot be flattened to JSON without losing its runtime meaning.
export function toPlainJson(value) {
  if (value.kind === 'literal') return value.value
  if (value.kind === 'object') return Object.fromEntries(Object.entries(value.fields).map(([key, child]) => [key, toPlainJson(child)]))
  if (value.kind === 'array') return value.items.map(toPlainJson)
  throw new Error('已绑定变量或步骤输出，请在字段配置中编辑')
}
export const authLabel = kind => ({ none: '无鉴权', bearer: 'Bearer Token', basic: 'Basic', api_key: 'API Key' }[kind] || '无鉴权')
export function blankConfig() {
  return {
    request: {
      method: 'GET', url: literal(''), path_params: [], query: [], headers: [], body_type: 'none', body: literal(null), form: [],
      auth: { kind: 'none', token: literal(), username: literal(), password: literal(), key_name: 'X-API-Key', location: 'header' }, timeout_seconds: 30,
    },
    assertions: [blankAssertion()], sensitive_paths: [],
  }
}
export const blankAssertion = () => ({ id: createUuid(), path: ['status_code'], op: 'is_2xx', expected: literal(null) })
export const standardFields = () => [
  { path: ['status_code'], type: 'number' }, { path: ['elapsed_ms'], type: 'number' },
  { path: ['headers'], type: 'object', children: [] }, { path: ['cookies'], type: 'object', children: [] },
  { path: ['body'], type: 'object', children: [] }, { path: ['text'], type: 'string' },
]
export const sampleTypes = fields => (fields || []).flatMap(f => [{ path: f.path, type: f.type }, ...sampleTypes(f.children)])
// Editor sample metadata is never supplied as execution outputs.
export function responseFields(response) {
  let remaining = 2000
  const walk = (value, path) => {
    if (remaining-- <= 0 || path.length > 32) return []
    const type = value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value === 'number' ? 'number' : typeof value
    const entries = type === 'array' ? value.map((child, index) => [index, child]) : type === 'object' ? Object.entries(value) : []
    return [{ path, type, ...(['object','array'].includes(type)?{}:{example:value}), children: entries.flatMap(([key, child]) => walk(child, [...path, key])) }]
  }
  return walk(response, [])[0]?.children || []
}
export function effectiveStep(step) {
  const result = copy(step.snapshot)
  const overrides = step.overrides || {}
  Object.assign(result.request, copy(overrides.request || {}))
  if (overrides.assertions) result.assertions = copy(overrides.assertions)
  if (overrides.sensitive_paths) result.sensitive_paths = copy(overrides.sensitive_paths)
  return result
}
export function overridesFor(snapshot, config) {
  const overrides = { request: {} }
  for (const key of Object.keys(config.request)) {
    if (JSON.stringify(config.request[key]) !== JSON.stringify(snapshot.request[key])) overrides.request[key] = copy(config.request[key])
  }
  for (const key of ['assertions', 'sensitive_paths']) {
    if (JSON.stringify(snapshot[key]) !== JSON.stringify(config[key])) overrides[key] = copy(config[key])
  }
  return overrides
}
export function stepFromInterface(item) {
  return { id: createUuid(), name: item.name, kind: 'request', interface_id: item.id, interface_version: item.version, snapshot: copy(item.config), overrides: {}, seconds: 1, retry_count: 0 }
}
export function localRequestStep(config = blankConfig(), name = '新请求') {
  return { id: createUuid(), name, kind: 'request', interface_id: null, interface_version: null, snapshot: copy(config), overrides: {}, seconds: 1, retry_count: 0 }
}
export function cloneStep(step) {
  return { ...copy(step), id: createUuid(), name: `${step.name} 副本` }
}
export const pathLabel = path => (path || []).map(k => typeof k === 'number' ? `[${k}]` : k).join(' › ')
// Mirrors backend api_testing.values.conflict_key. Section and request field
// names never contain dots, so joining keeps both sides byte-identical without
// depending on JSON whitespace.
export const conflictKey = path => (path || []).map(String).join('.')
export const referenceLabel = (value, sources = []) => value.kind === 'env' ? `环境 → ${value.name}` : `${sources.find(s => s.id === value.step_id)?.name || '失效步骤'} → ${fieldName(value.path)}`
export const fieldName = path => typeof path?.at(-1) === 'number' ? `[${path.at(-1)}]` : path?.at(-1) || '响应'
export const shortValue = value => {
  const text = JSON.stringify(value)
  return text === undefined ? '暂无示例' : text.length > 60 ? `${text.slice(0, 60)}…` : text
}
export const flattenFields = fields => (fields || []).flatMap(f => [f, ...flattenFields(f.children)])
export function mergeResponseFields(fields = []) {
  const byKey = new Map(fields.map(f => [JSON.stringify(f.path), f]))
  return standardFields().map(f => byKey.get(JSON.stringify(f.path)) || f)
}
export function requestSignatures(steps) {
  let chain = ''
  return Object.fromEntries(steps.map(step => {
    chain += JSON.stringify([step.id, step.kind, step.kind === 'wait' ? step.seconds : effectiveStep(step).request])
    return [step.id, chain]
  }))
}
export const assertionSignature = step => JSON.stringify(step.kind === 'request' ? effectiveStep(step).assertions : [])
export const sourceLabel = source => `${source?.origin || (source?.stale ? '上次调试' : '接口样例')}${source?.env_name ? ` · ${source.env_name}` : ''}${source?.captured_at ? ` · ${source.captured_at.replace('T', ' ').slice(0,19)}` : ''}`
export function previewValue(value, sources = []) {
  if (!value) return null
  if (value.kind === 'literal') return value.value
  if (value.kind === 'object') return Object.fromEntries(Object.entries(value.fields).map(([k,v]) => [k, previewValue(v, sources)]))
  if (value.kind === 'array') return value.items.map(v => previewValue(v, sources))
  if (value.kind === 'template') return value.parts.map(v => v.kind === 'literal' ? String(v.value ?? '') : `〈${referenceLabel(v,sources)} · 运行时取值〉`).join('')
  return `〈${referenceLabel(value,sources)} · 运行时取值〉`
}
export function requestPreview(request, sources = []) {
  return { 方法: request.method, 地址: previewValue(request.url, sources), 路径参数: request.path_params.filter(p=>p.enabled).map(p=>({名称:p.name,值:previewValue(p.value,sources)})), 查询参数: request.query.filter(p=>p.enabled).map(p=>({名称:p.name,值:previewValue(p.value,sources)})), 请求头: request.headers.filter(p=>p.enabled).map(p=>({名称:p.name,值:previewValue(p.value,sources)})), 鉴权: authLabel(request.auth.kind), 请求体: request.body_type === 'form' ? request.form.filter(p=>p.enabled).map(p=>({名称:p.name,值:previewValue(p.value,sources)})) : request.body_type === 'none' ? null : previewValue(request.body,sources) }
}
export function businessAssertion(field, realValue = false) {
  if (dynamicField(field.path)) {
    return { id:createUuid(), path:copy(field.path), op:'not_empty', expected:literal(null) }
  }
  return { id:createUuid(), path:copy(field.path), op:'eq', expected:realValue && Object.hasOwn(field,'example') ? fromJson(field.example) : literal('') }
}
// Shared by the automatic suggestion and the manual-edit warning so the two
// never drift apart. Names that change every run must not be pinned to a value.
const DYNAMIC_FIELD = /(?:^id$|_id$|Id$|ID$|token|uuid|nonce|timestamp|(?:^|_)time$|Time$|_at$|At$)/
export const dynamicField = path => DYNAMIC_FIELD.test(String(path?.at(-1) || ''))
// Computed once per step so the card does not deep-copy the same snapshot for
// every field it renders. The card is narrow, so it shows only the method and
// the check count.
export function stepSummary(step) {
  if (step.kind === 'wait') return `等待 ${step.seconds} 秒`
  const config = effectiveStep(step)
  return `${config.request.method} · ${config.assertions.length} 条校验`
}
export function referenceIssues(steps) {
  const available = new Set(), issues = []
  function walk(value, step, location = []) {
    if (!value || typeof value !== 'object') return
    if(value.kind === 'ref' && !available.has(value.step_id)) issues.push({step_id:step.id, step_name:step.name, source:value.step_id, location, section:location[0]==='assertions'?'assertions':({auth:'auth',headers:'headers',body:'body',form:'body'}[location[1]]||'params'),message:'引用的步骤不存在或不在前面'})
    for(const [key,child] of Object.entries(value)) if(child && typeof child==='object') {
      if(Array.isArray(child)) child.forEach((v,i)=>walk(v,step,[...location,key,i])); else walk(child,step,[...location,key])
    }
  }
  for(const step of steps) if(step.kind==='request'){
    const config=effectiveStep(step),request=config.request
    walk(request.url,step,['request','url'])
    for(const key of ['path_params','query','headers',...(request.body_type==='form'?['form']:[])])request[key].forEach((row,i)=>{if(row.enabled!==false)walk(row.value,step,['request',key,i,'value'])})
    if(['json','text'].includes(request.body_type))walk(request.body,step,['request','body'])
    for(const key of ({bearer:['token'],api_key:['token'],basic:['username','password']}[request.auth.kind]||[]))walk(request.auth[key],step,['request','auth',key])
    config.assertions.forEach((row,i)=>{if(!['exists','not_empty','is_2xx'].includes(row.op))walk(row.expected,step,['assertions',i,'expected'])})
    available.add(step.id)
  }
  return issues
}
export const locationLabel = location => (location || []).map(k=>({request:'请求',assertions:'断言',body:'请求体',headers:'请求头',query:'查询参数',path_params:'路径参数',url:'地址',auth:'鉴权',token:'凭证',expected:'期望值',fields:'字段',items:'数组',value:'值'}[k] || (typeof k==='number'?`第 ${k+1} 项`:k))).join(' › ')
export function failureSummary(run) {
  const index = run?.steps?.findIndex(s=>['FAIL','ERROR'].includes(s.status)) ?? -1
  if(index < 0) return run?.error || ''
  const step=run.steps[index], check=step.detail?.assertions?.find(a=>!a.passed)
  return `第 ${index+1} 步「${step.name}」${step.status==='FAIL'?'断言失败':'执行异常'}：${check ? `${pathLabel(check.path)} ${assertionLabel(check.op)} ${check.op==='is_2xx'?'200–299':shortValue(check.expected)}，实际${check.missing?'字段不存在':`为 ${shortValue(check.actual)}`}（${check.message}）` : step.detail?.error || '请查看步骤详情'}`
}
export function validateReferences(steps) {
  return [...new Set(referenceIssues(steps).map(issue=>`${issue.step_name}：${issue.message}`))]
}
// Which steps a batch AI pass can generate for, in order, and why the others
// cannot. Mirrors the server: a suggestion needs a real response from this
// debug session, the chain stops at the first non-PASS step, and an error
// response must never be turned into a template.
export function batchSuggestionPlan(steps, results = {}) {
  let blockedBy = null
  return (steps || []).map(step => {
    const base = { stepId: step.id, name: step.name }
    if (step.kind !== 'request') return { ...base, eligible: false, reason: 'wait' }
    const row = results[step.id]
    if (!row?.detail?.response) return { ...base, eligible: false, reason: 'not_executed', blockedBy }
    const eligible = ['PASS', 'UNCHECKED'].includes(row.status)
    if (row.status !== 'PASS' && !blockedBy) blockedBy = step.id
    return eligible ? { ...base, eligible: true, reason: null, status: row.status } : { ...base, eligible: false, reason: 'failed', status: row.status }
  })
}
export function apiError(error) {
  const detail = error.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map(v => v.message || v.msg || JSON.stringify(v)).join('；')
  if (detail?.message) return `${detail.message}：${(detail.scenarios || detail.tasks || []).join('、')}`
  return describeError(error) || '操作失败'
}
export const activeStatus = status => ['QUEUED', 'RUNNING', 'PENDING'].includes(status)
export const statusType = status => ({ PASS: 'success', FAIL: 'danger', ERROR: 'danger', UNCHECKED:'warning', RUNNING: 'primary', QUEUED: 'info', ABORTED: 'warning', SKIP: 'info' }[status] || 'info')
export const statusLabel = status => ({ PASS: '通过', FAIL: '校验失败', ERROR: '执行异常', UNCHECKED:'未校验', RUNNING: '运行中', QUEUED: '排队中', PENDING: '待执行', ABORTED: '已中止', SKIP: '已跳过', IDLE: '未运行' }[status] || status)
export const assertionLabel = op => ({ is_2xx: '状态码 2xx', eq: '等于', ne: '不等于', contains: '包含', exists: '存在', not_empty: '非空', gt: '大于', gte: '大于等于', lt: '小于', lte: '小于等于', type: '类型为', length: '长度等于' }[op] || op)
// Identity of a check (same path, condition and expected value). Server dumps
// and locally built literals differ in shape, so literals compare by value.
export const assertionKey = assertion => {
  const expected = assertion.expected
  const value = ['exists', 'not_empty', 'is_2xx'].includes(assertion.op) ? null
    : !expected ? null
    : expected.kind === 'literal' ? ['literal', expected.value ?? null]
    : expected
  return JSON.stringify([assertion.path, assertion.op, value])
}
// Applying AI suggestions must be idempotent: a second click never appends a
// check the step already has.
export function newAssertions(existing, additions) {
  const seen = new Set((existing || []).map(assertionKey))
  return (additions || []).filter(assertion => { const key = assertionKey(assertion); if (seen.has(key)) return false; seen.add(key); return true })
}
export async function downloadReport(api, id) {
  const { data } = await api.download(id)
  const url = URL.createObjectURL(data)
  const link = document.createElement('a')
  link.href = url; link.download = `api-report-${id}.html`; link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
