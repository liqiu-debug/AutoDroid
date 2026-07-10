/**
 * UI 层级 XML 解析与坐标换算工具（Android uiautomator / iOS WDA 双格式兼容）。
 */

const parseNumberAttr = (value) => {
  const parsed = Number.parseFloat(String(value ?? '').trim())
  return Number.isFinite(parsed) ? Math.round(parsed) : null
}

const resolveHierarchyScale = (deviceInfoPayload) => {
  const platform = String(deviceInfoPayload?.platform || '').trim().toLowerCase()
  if (platform !== 'ios') return 1
  const scale = Number.parseFloat(String(deviceInfoPayload?.scale ?? '1').trim())
  return Number.isFinite(scale) && scale > 0 ? scale : 1
}

const applyBoundsScale = (bounds, scale = 1) => {
  const safeScale = Number.isFinite(scale) && scale > 0 ? scale : 1
  if (safeScale === 1 || !bounds) return bounds
  return {
    x1: Math.round(bounds.x1 * safeScale),
    y1: Math.round(bounds.y1 * safeScale),
    x2: Math.round(bounds.x2 * safeScale),
    y2: Math.round(bounds.y2 * safeScale)
  }
}

export const parseNodeBounds = (node, scale = 1) => {
  const bounds = node.getAttribute('bounds')
  if (bounds) {
    const match = bounds.match(/\[(\d+),(\d+)\]\[(\d+),(\d+)\]/)
    if (match) {
      return applyBoundsScale({
        x1: Number.parseInt(match[1], 10),
        y1: Number.parseInt(match[2], 10),
        x2: Number.parseInt(match[3], 10),
        y2: Number.parseInt(match[4], 10)
      }, scale)
    }
  }

  const left = parseNumberAttr(node.getAttribute('left'))
  const top = parseNumberAttr(node.getAttribute('top'))
  const right = parseNumberAttr(node.getAttribute('right'))
  const bottom = parseNumberAttr(node.getAttribute('bottom'))
  if ([left, top, right, bottom].every(value => value !== null)) {
    return applyBoundsScale({ x1: left, y1: top, x2: right, y2: bottom }, scale)
  }

  const x = parseNumberAttr(node.getAttribute('x'))
  const y = parseNumberAttr(node.getAttribute('y'))
  const width = parseNumberAttr(node.getAttribute('width'))
  const height = parseNumberAttr(node.getAttribute('height'))
  if ([x, y, width, height].every(value => value !== null)) {
    return applyBoundsScale({
      x1: x,
      y1: y,
      x2: x + Math.max(width, 0),
      y2: y + Math.max(height, 0)
    }, scale)
  }

  if ([x, y, right, bottom].every(value => value !== null)) {
    return applyBoundsScale({ x1: x, y1: y, x2: right, y2: bottom }, scale)
  }

  return null
}

const shouldSkipCanvasNode = (className, depth) => {
  const normalizedClassName = String(className || '').trim()
  if (!normalizedClassName) return false
  if (normalizedClassName === 'AppiumAUT' || normalizedClassName === 'hierarchy') return true
  if (normalizedClassName.includes('XCUIElementTypeApplication')) return true
  if (normalizedClassName.includes('XCUIElementTypeWindow') && depth <= 2) return true
  return false
}

// Parse XML hierarchy into node list with bounds
export const parseHierarchy = (xml, deviceInfoPayload = null) => {
  if (!xml) return []
  const parser = new DOMParser()
  const doc = parser.parseFromString(xml, 'text/xml')
  const result = []
  const coordinateScale = resolveHierarchyScale(deviceInfoPayload)

  const traverse = (node, depth = 0) => {
    if (node.nodeType === 1) {
      const bounds = parseNodeBounds(node, coordinateScale)
      if (bounds) {
        const { x1, y1, x2, y2 } = bounds
        const hasElementChildren = Array.from(node.childNodes || []).some(child => child.nodeType === 1)
        const className = node.getAttribute('class') || node.getAttribute('type') || node.nodeName || ''
        if (!shouldSkipCanvasNode(className, depth)) {
          result.push({
            x1,
            y1,
            x2,
            y2,
            text: node.getAttribute('text') || node.getAttribute('label') || node.getAttribute('value') || '',
            resourceId: node.getAttribute('resource-id') || node.getAttribute('resourceId') || node.getAttribute('id') || '',
            className,
            contentDesc: node.getAttribute('content-desc') || node.getAttribute('name') || '',
            depth,
            childCount: Array.from(node.childNodes || []).filter(child => child.nodeType === 1).length,
            isLeaf: !hasElementChildren,
            area: Math.max(0, x2 - x1) * Math.max(0, y2 - y1)
          })
        }
      }
    }
    for (const child of node.childNodes) {
      traverse(child, depth + 1)
    }
  }
  traverse(doc.documentElement)
  return result
}

export const parseResolution = (resolution) => {
  const match = String(resolution || '').match(/(\d+)\s*x\s*(\d+)/i)
  if (!match) return { width: 0, height: 0 }
  return {
    width: Number(match[1]) || 0,
    height: Number(match[2]) || 0
  }
}
