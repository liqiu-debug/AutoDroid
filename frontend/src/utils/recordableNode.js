const DYNAMIC_TIME_RANGE_PATTERN = /^[\[\(]?\s*\d{1,2}:\d{2}\s*[,/\-~]\s*\d{1,2}:\d{2}\s*[\]\)]?$/

const normalizeLocatorText = (raw) => String(raw || '').trim()

const isUnstableLocatorText = (raw) => {
  const text = normalizeLocatorText(raw)
  if (!text) return false
  return DYNAMIC_TIME_RANGE_PATTERN.test(text)
}

export const getStableNodeText = (node) => {
  const text = normalizeLocatorText(node?.text)
  return isUnstableLocatorText(text) ? '' : text
}

export const getStableNodeDescription = (node) => {
  const desc = normalizeLocatorText(node?.contentDesc)
  return isUnstableLocatorText(desc) ? '' : desc
}

export const isRecordableSemanticNode = (node) => {
  return Boolean(getStableNodeText(node) || getStableNodeDescription(node))
}

const getNodeArea = (node) => {
  const width = Math.max(0, Number(node?.x2 || 0) - Number(node?.x1 || 0))
  const height = Math.max(0, Number(node?.y2 || 0) - Number(node?.y1 || 0))
  return Number(node?.area) || width * height
}

const isLayoutNode = (node) => {
  const className = String(node?.className || '')
  return ['Layout', 'ViewGroup', 'ScrollView'].some((keyword) => className.includes(keyword))
    || className === 'View'
}

const containsPoint = (node, realX, realY) => {
  return realX >= Number(node?.x1 || 0)
    && realX <= Number(node?.x2 || 0)
    && realY >= Number(node?.y1 || 0)
    && realY <= Number(node?.y2 || 0)
}

const isGlobalContainerNode = (node, maxArea) => {
  const nodeArea = getNodeArea(node)
  const safeMaxArea = Math.max(Number(maxArea) || 0, 1)
  const areaRatio = nodeArea / safeMaxArea
  const depth = Number(node?.depth ?? 99)
  const childCount = Number(node?.childCount ?? 0)
  const className = String(node?.className || '')
  const resourceId = String(node?.resourceId || '').trim()
  const hasSemanticLabel = Boolean(getStableNodeText(node) || getStableNodeDescription(node))

  if (areaRatio >= 0.55 && depth <= 1 && !node?.isLeaf) return true
  if (areaRatio < 0.72) return false

  if (className === 'AppiumAUT' || className === 'hierarchy') return true
  if (className.includes('XCUIElementTypeApplication')) return true
  if (className.includes('XCUIElementTypeWindow') && depth <= 2) return true
  if (
    !node?.isLeaf
    && childCount > 0
    && /(XCUIElementTypeOther|XCUIElementTypeScrollView|XCUIElementTypeCollectionView|XCUIElementTypeTable|XCUIElementTypeWebView|android\.webkit\.WebView|ScrollView|RecyclerView|ListView|CollectionView|TableView|ViewGroup|FrameLayout|CoordinatorLayout|ConstraintLayout|LinearLayout|RelativeLayout)/.test(className)
  ) {
    return true
  }
  if (!resourceId && hasSemanticLabel && depth <= 3 && !node?.isLeaf) return true

  if (
    depth <= 1
    && /(DecorView|FrameLayout|ViewGroup|CoordinatorLayout|ConstraintLayout|LinearLayout|RelativeLayout)/.test(className)
  ) {
    return true
  }

  return false
}

const filterPointCandidates = (nodes, realX, realY) => {
  const source = Array.isArray(nodes) ? nodes : []
  const maxArea = source.reduce((largest, node) => Math.max(largest, getNodeArea(node)), 0)

  return source
    .filter((node) => containsPoint(node, realX, realY))
    .filter((node) => !isGlobalContainerNode(node, maxArea))
}

const compareScores = (left, right) => {
  for (let index = 0; index < left.length; index += 1) {
    if (left[index] !== right[index]) {
      return left[index] - right[index]
    }
  }
  return 0
}

const buildNodeScore = (node) => {
  const hasText = Boolean(getStableNodeText(node))
  return [
    hasText ? 0 : 1,
    isLayoutNode(node) ? 1 : 0,
    node?.isLeaf ? 0 : 1,
    getNodeArea(node)
  ]
}

const buildAnyNodeScore = (node) => {
  return [
    isLayoutNode(node) ? 1 : 0,
    node?.isLeaf ? 0 : 1,
    getNodeArea(node)
  ]
}

const sortByScore = (candidates, buildScore) => {
  candidates.sort((left, right) => compareScores(buildScore(left), buildScore(right)))
  return candidates[0]
}

export const findBestNodeAtPoint = (nodes, realX, realY) => {
  const candidates = filterPointCandidates(nodes, realX, realY)

  if (!candidates.length) return null

  return sortByScore(candidates, buildAnyNodeScore)
}

export const findBestRecordableNode = (nodes, realX, realY) => {
  const candidates = filterPointCandidates(nodes, realX, realY)
    .filter((node) => isRecordableSemanticNode(node))

  if (!candidates.length) return null

  return sortByScore(candidates, buildNodeScore)
}
