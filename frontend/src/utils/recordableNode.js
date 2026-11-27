import { formatLocatorTitle, serializeCompositeConditions } from './compositeLocator.js'

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

const ALWAYS_NOISE_IDS = new Set([
  'android:id/content',
  'android:id/statusBarBackground',
  'android:id/navigationBarBackground',
  'android:id/background',
  'android:id/list',
  'android:id/empty',
])
const ALWAYS_NOISE_ID_SUFFIXES = [
  ':id/action_bar_root',
  ':id/decor_content_parent',
  ':id/action_bar_container',
]
const CONTAINER_NOISE_ID_SUFFIXES = [
  ':id/content',
  ':id/root',
  ':id/container',
  ':id/react_root_view',
  ':id/flutter_view',
]
const AUTO_GENERATED_ID_PATTERN = /(?:^|:id\/)(?:0x[0-9a-fA-F]+|\d+)$/
const GENERIC_LAYOUT_SHORT_NAMES = new Set([
  'View',
  'ViewGroup',
  'FrameLayout',
  'LinearLayout',
  'RelativeLayout',
  'ConstraintLayout',
  'CoordinatorLayout',
  'ScrollView',
  'HorizontalScrollView',
  'NestedScrollView',
  'RecyclerView',
  'ListView',
  'GridView',
  'WebView',
  'DecorView',
  'AppiumAUT',
  'hierarchy',
])

const isGenericLayoutClass = (className) => {
  const cls = String(className || '').trim()
  if (!cls) return true
  if (IOS_STRUCTURAL_TYPES.has(cls) || cls === 'XCUIElementTypeOther') return true
  const dotIdx = cls.lastIndexOf('.')
  const shortName = dotIdx >= 0 ? cls.slice(dotIdx + 1) : cls
  if (GENERIC_LAYOUT_SHORT_NAMES.has(shortName)) return true
  return ['Layout', 'ViewGroup', 'ScrollView'].some((kw) => cls.includes(kw))
}

const isUsableResourceId = (rawId, { isLeaf, areaRatio }) => {
  const rid = String(rawId || '').trim()
  if (!rid) return false
  const ridLower = rid.toLowerCase()
  if (ALWAYS_NOISE_IDS.has(rid) || ALWAYS_NOISE_IDS.has(ridLower)) return false
  if (ALWAYS_NOISE_ID_SUFFIXES.some((suffix) => ridLower.endsWith(suffix))) return false
  if (AUTO_GENERATED_ID_PATTERN.test(rid)) return false
  if (!isLeaf && areaRatio >= 0.45 && CONTAINER_NOISE_ID_SUFFIXES.some((suffix) => ridLower.endsWith(suffix))) {
    return false
  }
  return true
}

const nodeIndexCache = new WeakMap()

export const getOrBuildNodeIndex = (nodes) => {
  if (!Array.isArray(nodes)) return null
  const cached = nodeIndexCache.get(nodes)
  if (cached && cached.length === nodes.length) return cached

  const maxArea = Math.max(getMaxArea(nodes), 1)
  const byId = new Map()
  const byText = new Map()
  const byDesc = new Map()
  const byClass = new Map()

  const addBucket = (map, key, idx) => {
    if (!key) return
    let list = map.get(key)
    if (!list) {
      list = []
      map.set(key, list)
    }
    list.push(idx)
  }

  const meta = nodes.map((node, idx) => {
    const area = getNodeArea(node)
    const areaRatio = area / maxArea
    const rawId = String(node?.resourceId || '').trim()
    const rawText = normalizeLocatorText(node?.text)
    const rawDesc = normalizeLocatorText(node?.contentDesc)
    const className = String(node?.className || '').trim()
    const cleanText = isUnstableLocatorText(rawText) ? '' : rawText
    const cleanDesc = isUnstableLocatorText(rawDesc) ? '' : rawDesc
    const usableId = isUsableResourceId(rawId, { isLeaf: Boolean(node?.isLeaf), areaRatio }) ? rawId : ''

    addBucket(byId, rawId, idx)
    addBucket(byText, rawText, idx)
    addBucket(byDesc, rawDesc, idx)
    addBucket(byClass, className, idx)

    return {
      idx,
      node,
      area,
      areaRatio,
      rawId,
      usableId,
      rawText,
      cleanText,
      rawDesc,
      cleanDesc,
      className,
      isLeaf: Boolean(node?.isLeaf),
      clickable: Boolean(node?.clickable),
      hasEuler: Number.isFinite(node?.enter) && Number.isFinite(node?.exit),
    }
  })

  const indexData = {
    length: nodes.length,
    maxArea,
    meta,
    buckets: {
      id: byId,
      text: byText,
      description: byDesc,
      class_name: byClass,
    },
  }
  nodeIndexCache.set(nodes, indexData)
  return indexData
}

const isSameLineage = (aMeta, bMeta) => {
  if (aMeta.idx === bMeta.idx) return true
  if (aMeta.hasEuler && bMeta.hasEuler) {
    const aEnter = aMeta.node.enter
    const aExit = aMeta.node.exit
    const bEnter = bMeta.node.enter
    const bExit = bMeta.node.exit
    return (aEnter <= bEnter && bExit <= aExit) || (bEnter <= aEnter && aExit <= bExit)
  }
  // 兼容未带 enter/exit 的手写测试节点：按包围盒包含判定谱系关系
  const a = aMeta.node
  const b = bMeta.node
  const aContainsB = a.x1 <= b.x1 && a.y1 <= b.y1 && a.x2 >= b.x2 && a.y2 >= b.y2
  const bContainsA = b.x1 <= a.x1 && b.y1 <= a.y1 && b.x2 >= a.x2 && b.y2 >= a.y2
  return aContainsB || bContainsA
}

const isUniqueTargetInIndex = (indexData, conditions, targetMeta) => {
  const lists = []
  for (const [key, value] of Object.entries(conditions)) {
    if (!value) return false
    const bucket = indexData.buckets[key]
    if (!bucket) return false
    const matched = bucket.get(value)
    if (!matched || matched.length === 0) return false
    lists.push(matched)
  }
  if (lists.length === 0) return false

  lists.sort((a, b) => a.length - b.length)
  let candidates = new Set(lists[0])
  for (let i = 1; i < lists.length; i += 1) {
    const nextSet = new Set(lists[i])
    candidates = new Set([...candidates].filter((idx) => nextSet.has(idx)))
    if (candidates.size === 0) return false
  }
  if (!candidates.has(targetMeta.idx)) return false

  for (const idx of candidates) {
    if (!isSameLineage(targetMeta, indexData.meta[idx])) {
      return false
    }
  }
  return true
}

const serializeCompositeSpec = (pairs) => {
  return serializeCompositeConditions(pairs.map(([key, value]) => ({ key, value })))
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

  const maxArea = Math.max(getMaxArea(source), 1)
  const croppable = candidates.filter((node) => isUsableImageTemplate(node, maxArea))
  if (croppable.length) return sortByScore(croppable, buildAnyNodeScore)
  // Large Android View containers need the same local selection as iOS.
  if (candidates.length || source.some(isIosNode)) return buildIosTapAreaNode(realX, realY)
  return null
}

export const evaluateRecordableNode = (nodes, realX, realY, locatorConfig = null) => {
  const cfg = locatorConfig && typeof locatorConfig === 'object' ? locatorConfig : {}
  const enableResourceId = Boolean(cfg.enable_resource_id)
  const enableClassName = Boolean(cfg.enable_class_name)
  const priority = cfg.priority === 'id_first' && enableResourceId ? 'id_first' : 'semantic_first'

  if (!enableResourceId && !enableClassName) {
    const candidates = filterPointCandidates(nodes, realX, realY)
      .filter((node) => isRecordableSemanticNode(node))
    if (!candidates.length) return null
    const best = sortByScore(candidates, buildNodeScore)
    const txt = getStableNodeText(best)
    const strategy = txt ? 'text' : 'description'
    const selector = txt || getStableNodeDescription(best)
    return {
      node: best,
      strategy,
      selector,
      previewLabel: formatLocatorTitle(strategy, selector),
    }
  }

  const indexData = getOrBuildNodeIndex(nodes)
  if (!indexData) return null

  const hitNodes = filterPointCandidates(nodes, realX, realY)
  if (!hitNodes.length) return null

  const hitSet = new Set(hitNodes)
  const hitMetas = indexData.meta.filter((m) => hitSet.has(m.node))
  const synthesizedHits = []

  for (const item of hitMetas) {
    const txt = item.cleanText
    const desc = item.cleanDesc
    const rid = enableResourceId ? item.usableId : ''
    const cls = enableClassName ? item.className : ''
    const nonLayoutCls = cls && !isGenericLayoutClass(cls) ? cls : ''

    if (
      !txt
      && !desc
      && isGenericLayoutClass(item.className)
      && !item.isLeaf
      && (item.areaRatio >= 0.30 || (!item.clickable && item.areaRatio >= 0.08))
    ) {
      continue
    }

    const trySpec = (conditions, strategy, selector, tier) => {
      if (isUniqueTargetInIndex(indexData, conditions, item)) {
        return { tier, strategy, selector }
      }
      return null
    }

    const trySemanticUnique = (tierBase) => {
      if (txt) {
        const res = trySpec({ text: txt }, 'text', txt, tierBase)
        if (res) return res
      }
      if (desc) {
        const res = trySpec({ description: desc }, 'description', desc, tierBase)
        if (res) return res
      }
      return null
    }

    const trySemanticDisambiguate = (tierBase) => {
      if (txt) {
        if (rid) {
          const res = trySpec({ id: rid, text: txt }, 'composite', serializeCompositeSpec([['id', rid], ['text', txt]]), tierBase)
          if (res) return res
          const resId = trySpec({ id: rid }, 'resourceId', rid, tierBase)
          if (resId) return resId
        }
        if (cls) {
          const res = trySpec({ class_name: cls, text: txt }, 'composite', serializeCompositeSpec([['class_name', cls], ['text', txt]]), tierBase)
          if (res) return res
        }
        if (rid && cls) {
          const res = trySpec({ class_name: cls, id: rid, text: txt }, 'composite', serializeCompositeSpec([['class_name', cls], ['id', rid], ['text', txt]]), tierBase)
          if (res) return res
        }
      }
      if (desc) {
        if (rid) {
          const res = trySpec({ description: desc, id: rid }, 'composite', serializeCompositeSpec([['description', desc], ['id', rid]]), tierBase)
          if (res) return res
          const resId = trySpec({ id: rid }, 'resourceId', rid, tierBase)
          if (resId) return resId
        }
        if (cls) {
          const res = trySpec({ class_name: cls, description: desc }, 'composite', serializeCompositeSpec([['class_name', cls], ['description', desc]]), tierBase)
          if (res) return res
        }
        if (rid && cls) {
          const res = trySpec({ class_name: cls, description: desc, id: rid }, 'composite', serializeCompositeSpec([['class_name', cls], ['description', desc], ['id', rid]]), tierBase)
          if (res) return res
        }
      }
      return null
    }

    const tryIdAndCombinations = (tierBase) => {
      if (!rid) return null
      const resId = trySpec({ id: rid }, 'resourceId', rid, tierBase)
      if (resId) return resId
      if (txt) {
        const res = trySpec({ id: rid, text: txt }, 'composite', serializeCompositeSpec([['id', rid], ['text', txt]]), tierBase)
        if (res) return res
      }
      if (desc) {
        const res = trySpec({ description: desc, id: rid }, 'composite', serializeCompositeSpec([['description', desc], ['id', rid]]), tierBase)
        if (res) return res
      }
      if (cls) {
        const resCls = trySpec({ class_name: cls, id: rid }, 'composite', serializeCompositeSpec([['class_name', cls], ['id', rid]]), tierBase)
        if (resCls) return resCls
        if (txt) {
          const res = trySpec({ class_name: cls, id: rid, text: txt }, 'composite', serializeCompositeSpec([['class_name', cls], ['id', rid], ['text', txt]]), tierBase)
          if (res) return res
        }
        if (desc) {
          const res = trySpec({ class_name: cls, description: desc, id: rid }, 'composite', serializeCompositeSpec([['class_name', cls], ['description', desc], ['id', rid]]), tierBase)
          if (res) return res
        }
      }
      return null
    }

    let hit = null
    if (priority === 'id_first') {
      hit = tryIdAndCombinations(0) || trySemanticUnique(1) || trySemanticDisambiguate(1)
    } else {
      hit = trySemanticUnique(0) || trySemanticDisambiguate(1) || tryIdAndCombinations(1)
    }
    if (!hit && nonLayoutCls) {
      hit = trySpec({ class_name: nonLayoutCls }, 'className', nonLayoutCls, 2)
    }
    if (hit) {
      const score = [
        hit.tier,
        isGenericLayoutClass(item.className) ? 1 : 0,
        item.isLeaf ? 0 : 1,
        item.area,
      ]
      synthesizedHits.push({ ...hit, item, score })
    }
  }

  if (!synthesizedHits.length) return null
  synthesizedHits.sort((a, b) => compareScores(a.score, b.score))
  const best = synthesizedHits[0]
  return {
    node: best.item.node,
    strategy: best.strategy,
    selector: best.selector,
    previewLabel: formatLocatorTitle(best.strategy, best.selector),
  }
}

export const findBestRecordableNode = (nodes, realX, realY, locatorConfig = null) => {
  const evaluated = evaluateRecordableNode(nodes, realX, realY, locatorConfig)
  return evaluated ? evaluated.node : null
}
