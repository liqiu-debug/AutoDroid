/**
 * 前端组合定位 (by="composite") 解析、序列化与展示工具。
 * 与后端 backend/utils/composite_locator.py 保持契约一致。
 */

export const ALLOWED_COMPOSITE_KEYS = ['id', 'text', 'description', 'class_name']

export const COMPOSITE_FIELD_OPTIONS = [
  { value: 'id', label: 'Resource ID' },
  { value: 'text', label: 'Text' },
  { value: 'description', label: 'Description' },
  { value: 'class_name', label: 'Class Name' },
]

const KEY_ALIASES = {
  id: 'id',
  resourceid: 'id',
  resource_id: 'id',
  'resource-id': 'id',
  accessibilityid: 'id',
  accessibility_id: 'id',
  text: 'text',
  label: 'text',
  description: 'description',
  desc: 'description',
  'content-desc': 'description',
  content_desc: 'description',
  contentdesc: 'description',
  name: 'description',
  class_name: 'class_name',
  classname: 'class_name',
  class: 'class_name',
  type: 'class_name',
}

const KEY_SHORT_LABELS = {
  id: 'ID',
  text: 'Text',
  description: 'Desc',
  class_name: 'Class',
}

export function normalizeCompositeKey(rawKey) {
  return KEY_ALIASES[String(rawKey || '').trim().toLowerCase()] || ''
}

export function parseCompositeObject(raw) {
  if (!raw) return null
  let parsed = raw
  if (typeof raw === 'string') {
    const trimmed = raw.trim()
    if (!trimmed) return null
    try {
      parsed = JSON.parse(trimmed)
    } catch {
      return null
    }
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return null
  }

  const normalized = {}
  for (const [rawKey, rawVal] of Object.entries(parsed)) {
    const canonical = normalizeCompositeKey(rawKey)
    if (!canonical) return null
    if (rawVal === null || rawVal === undefined) continue
    if (typeof rawVal === 'object' || typeof rawVal === 'boolean') return null
    const val = String(rawVal).trim()
    if (!val) continue
    if (canonical in normalized && normalized[canonical] !== val) return null
    normalized[canonical] = val
  }
  return Object.keys(normalized).length > 0 ? normalized : null
}

export function parseCompositeConditions(raw) {
  const obj = parseCompositeObject(raw)
  if (!obj) return []
  return ALLOWED_COMPOSITE_KEYS
    .filter((key) => key in obj)
    .map((key) => ({ key, value: obj[key] }))
}

export function serializeCompositeConditions(rows) {
  if (!Array.isArray(rows)) return ''
  const payload = {}
  for (const key of ALLOWED_COMPOSITE_KEYS) {
    const row = rows.find((item) => normalizeCompositeKey(item?.key) === key)
    const val = String(row?.value ?? '').trim()
    if (val) {
      payload[key] = val
    }
  }
  if (Object.keys(payload).length === 0) return ''
  const sortedKeys = Object.keys(payload).sort()
  const sortedObj = {}
  for (const key of sortedKeys) {
    sortedObj[key] = payload[key]
  }
  return JSON.stringify(sortedObj)
}

export function isValidCompositeSelector(raw) {
  return parseCompositeObject(raw) !== null
}

export function shortenResourceId(resourceId) {
  const text = String(resourceId || '').trim()
  if (!text) return ''
  const idx = text.lastIndexOf(':id/')
  if (idx >= 0) return text.slice(idx + 4)
  const slashIdx = text.lastIndexOf('/')
  if (slashIdx >= 0) return text.slice(slashIdx + 1)
  return text
}

export function shortenClassName(className) {
  const text = String(className || '').trim()
  if (!text) return ''
  const dotIdx = text.lastIndexOf('.')
  return dotIdx >= 0 ? text.slice(dotIdx + 1) : text
}

export function formatLocatorTitle(byOrSelectorType, selector) {
  const rawSelector = String(selector || '').trim()
  if (!rawSelector) return '?'
  const by = String(byOrSelectorType || '').trim().toLowerCase()

  if (by === 'composite') {
    const rows = parseCompositeConditions(rawSelector)
    if (!rows.length) return rawSelector
    return rows
      .map(({ key, value }) => {
        const prefix = KEY_SHORT_LABELS[key] || key
        if (key === 'id') return `${prefix}:${shortenResourceId(value)}`
        if (key === 'class_name') return `${prefix}:${shortenClassName(value)}`
        return `${prefix}:${value}`
      })
      .join(' + ')
  }

  if (by === 'id' || by === 'resourceid' || by === 'resource_id') {
    return `ID:${shortenResourceId(rawSelector)}`
  }
  if (by === 'class_name' || by === 'classname' || by === 'class') {
    return `Class:${shortenClassName(rawSelector)}`
  }
  return rawSelector
}
