const DYNAMIC_TIME_RANGE_PATTERN = /^[\[\(]?\s*\d{1,2}:\d{2}\s*[,/\-~]\s*\d{1,2}:\d{2}\s*[\]\)]?$/
// iOS SwiftUI 容器把运行时类名当 name（如 _TtGC7SwiftUI32NavigationStackHosting），不是界面文案
const SWIFT_RUNTIME_NAME_PREFIX = '_Tt'

// iOS 无障碍树里的布局容器也带 label / name（工具栏、列表名、页面标识），但它们不是可点元素：
// 对应 Android 的布局节点。点在这些区域上要走图像点击，不能录成「点击 工具栏」。
const IOS_TYPE_PREFIX = 'XCUIElementType'
const IOS_STRUCTURAL_TYPES = new Set([
  'XCUIElementTypeWindow',
  'XCUIElementTypeToolbar',
  'XCUIElementTypeNavigationBar',
  'XCUIElementTypeTabBar',
  'XCUIElementTypeScrollView',
  'XCUIElementTypeTable',
  'XCUIElementTypeCollectionView',
  'XCUIElementTypeWebView',
  'XCUIElementTypeStatusBar',
  'XCUIElementTypeMap' // label 是实时相机参数 JSON，回放时已变化
])
const IOS_SCROLL_BAR_PATTERN = /(垂直|水平)滚动条|(vertical|horizontal) scroll bar/i
// 图像点击默认选区：节点本身能当模板才用它（够小、不是细长条）。整条装饰层 / 滚动条截出来的模板
// 中心离点击位置很远，回放会点错，改为框选点击位置附近
const IOS_FALLBACK_MAX_AREA_RATIO = 0.06
const IOS_FALLBACK_MAX_ASPECT_RATIO = 12
const IOS_TAP_AREA_HALF_SIZE_PX = 80

const normalizeLocatorText = (raw) => String(raw || '').trim()

const isUnstableLocatorText = (raw) => {
  const text = normalizeLocatorText(raw)
  if (!text) return false
  if (text.startsWith(SWIFT_RUNTIME_NAME_PREFIX)) return true
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
  const area = Number(node?.area)
  return Number.isFinite(area) ? area : width * height
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

const isIosNode = (node) => String(node?.className || '').startsWith(IOS_TYPE_PREFIX)

const isIosNonElementNode = (node) => {
  const className = String(node?.className || '')
  if (IOS_STRUCTURAL_TYPES.has(className)) return true
  if (className !== 'XCUIElementTypeOther') return false
  if (IOS_SCROLL_BAR_PATTERN.test(`${node?.text || ''} ${node?.contentDesc || ''}`)) return true
  // 有子节点、只有 name（开发者标识，如 HomeView）没有 label：相当于 Android 只有 resource-id 的布局
  return !node?.isLeaf && !getStableNodeText(node)
}

// iOS 根节点不进节点列表，占屏比以 parseHierarchy 带的屏幕面积为准，否则简单页面的小元素也算「铺满屏」
const getMaxArea = (nodes) => nodes.reduce(
  (largest, node) => Math.max(largest, getNodeArea(node), Number(node?.screenArea) || 0),
  0
)

const filterPointCandidates = (nodes, realX, realY) => {
  const source = Array.isArray(nodes) ? nodes : []
  const maxArea = getMaxArea(source)

  return source
    .filter((node) => containsPoint(node, realX, realY))
    .filter((node) => !isGlobalContainerNode(node, maxArea))
    .filter((node) => !isIosNonElementNode(node))
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

const isUsableImageTemplate = (node, screenArea) => {
  const width = Math.max(0, Number(node?.x2 || 0) - Number(node?.x1 || 0))
  const height = Math.max(0, Number(node?.y2 || 0) - Number(node?.y1 || 0))
  if (!width || !height) return false
  // 用完整尺寸而不是屏幕内面积：露出一小截的长容器同样截不出好模板
  return (width * height) / screenArea <= IOS_FALLBACK_MAX_AREA_RATIO
    && Math.max(width, height) / Math.min(width, height) <= IOS_FALLBACK_MAX_ASPECT_RATIO
}

/** iOS 点在空白处：没有可用节点时以点击位置为中心给一块区域，同样进入图像点击 */
const buildIosTapAreaNode = (realX, realY) => {
  const x1 = Math.max(0, realX - IOS_TAP_AREA_HALF_SIZE_PX)
  const y1 = Math.max(0, realY - IOS_TAP_AREA_HALF_SIZE_PX)
  const x2 = realX + IOS_TAP_AREA_HALF_SIZE_PX
  const y2 = realY + IOS_TAP_AREA_HALF_SIZE_PX
  return { x1, y1, x2, y2, text: '', contentDesc: '', resourceId: '', className: '', isLeaf: true, area: (x2 - x1) * (y2 - y1) }
}

export const findBestNodeAtPoint = (nodes, realX, realY) => {
  const candidates = filterPointCandidates(nodes, realX, realY)
  const source = Array.isArray(nodes) ? nodes : []

  if (source.some(isIosNode)) {
    const maxArea = Math.max(getMaxArea(source), 1)
    const croppable = candidates.filter((node) => isUsableImageTemplate(node, maxArea))
    return croppable.length ? sortByScore(croppable, buildAnyNodeScore) : buildIosTapAreaNode(realX, realY)
  }

  if (!candidates.length) return null

  return sortByScore(candidates, buildAnyNodeScore)
}

export const findBestRecordableNode = (nodes, realX, realY) => {
  const candidates = filterPointCandidates(nodes, realX, realY)
    .filter((node) => isRecordableSemanticNode(node))

  if (!candidates.length) return null

  return sortByScore(candidates, buildNodeScore)
}
