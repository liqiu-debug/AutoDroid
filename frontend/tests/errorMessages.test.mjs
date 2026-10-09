import test from 'node:test'
import assert from 'node:assert/strict'
import { describeError, describeDetail, normalizeError, getErrorDetail, isValidationList } from '../src/utils/errors.js'
import { apiError } from '../src/utils/apiTesting.js'

const http = (status, data, extra = {}) => ({ message: `Request failed with status code ${status}`, response: { status, data }, ...extra })

test('axios transport failures become actionable Chinese sentences', () => {
  assert.equal(describeError({ message: 'Network Error', request: {} }), '无法连接服务器，请检查网络或服务状态')
  assert.equal(describeError({ message: 'timeout of 30000ms exceeded', code: 'ECONNABORTED', request: {} }), '请求超时，请检查网络后重试')
  assert.equal(describeError({ name: 'CanceledError', code: 'ERR_CANCELED', message: 'canceled' }), '')
  assert.equal(describeError(null), '操作失败')
})

test('HTTP failures prefer the server detail and fall back to a status sentence', () => {
  assert.equal(describeError(http(400, { detail: '用户名或密码错误' })), '用户名或密码错误')
  assert.equal(describeError(http(500, '<!doctype html><html>…')), '服务内部错误，请稍后重试')
  assert.equal(describeError(http(502, '')), '服务暂时不可用，请稍后重试')
  assert.equal(describeError(http(418, { detail: null })), '请求失败（HTTP 418）')
  assert.equal(describeError(http(409, { detail: { message: '接口被场景引用，不能删除', scenarios: ['下单', '退款'] } })), '接口被场景引用，不能删除：下单、退款')
})

test('FastAPI validation lists are flattened; application lists keep their messages', () => {
  const pydantic = [{ loc: ['body', 'name'], msg: 'name：缺少必填项', type: 'missing' }, { loc: ['body', 'kind'], msg: 'kind：取值不在允许范围内', type: 'literal_error' }]
  assert.ok(isValidationList(pydantic))
  assert.equal(describeDetail(pydantic), 'name：缺少必填项；kind：取值不在允许范围内')
  const precheck = [{ step_id: 'a', location: ['request', 'url'], message: '请求 URL 格式无效' }]
  assert.ok(!isValidationList(precheck))
  assert.equal(describeDetail(precheck), '请求 URL 格式无效')
})

test('normalizeError rewrites message and detail in place without breaking structured details', () => {
  const english = http(422, { detail: [{ loc: ['body', 'limit'], msg: 'limit：必须大于或等于 1', type: 'greater_than_equal' }] })
  normalizeError(english)
  assert.equal(english.message, 'limit：必须大于或等于 1')
  assert.equal(english.response.data.detail, 'limit：必须大于或等于 1')

  const precheck = http(422, { detail: [{ step_id: 'a', message: '必填值不能为空' }] })
  normalizeError(precheck)
  assert.ok(Array.isArray(precheck.response.data.detail), 'application lists survive')
  assert.equal(precheck.message, '必填值不能为空')
  assert.equal(apiError(precheck), '必填值不能为空')

  const html = http(502, '<html>Bad Gateway</html>')
  normalizeError(html)
  assert.deepEqual(html.response.data, { detail: '服务暂时不可用，请稍后重试' })
  assert.equal(html.message, '服务暂时不可用，请稍后重试')

  const network = { message: 'Network Error', request: {} }
  normalizeError(network)
  assert.equal(network.message, '无法连接服务器，请检查网络或服务状态')
  assert.equal(getErrorDetail(network), '无法连接服务器，请检查网络或服务状态')

  const structured = http(409, { detail: { message: '场景被定时任务引用，不能删除', tasks: ['每日回归'] } })
  normalizeError(structured)
  assert.equal(typeof structured.response.data.detail, 'object', 'objects are kept for callers that read their fields')
  assert.equal(apiError(structured), '场景被定时任务引用，不能删除：每日回归')
})
