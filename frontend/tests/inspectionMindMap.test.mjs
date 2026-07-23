import assert from 'node:assert/strict'
import test from 'node:test'

import {
  actionTargetLabel,
  aggregateInspectionMindMap,
  applyMindMapCollapseState,
  assignInspectionDisplayLabels,
  boundedMindMapExportSize,
  buildInspectionMindMap,
  centerMindMapViewport,
  collectExpandableMindMapNodes,
  fitMindMapViewport,
  focusedMindMapScale,
  inspectionMindMapAggregationSignature,
  inspectionMindMapInitialFocusId,
  inspectionMindMapNodeNeedsAttention,
  inspectionMindMapNodePositions,
  mindMapScrollOptions,
  inspectionThumbnailAssetRequest,
  inspectionThumbnailSymbolSize,
  PAGE_TREE_LINE_STYLE,
  zoomMindMapViewport,
} from '../src/utils/inspectionMindMap.js'
import {
  inspectionActionStatus,
  inspectionActionStatusMeta,
  inspectionAssetAvailabilityLabel,
  inspectionCaptureKindLabel,
  inspectionExecutionDispositionLabel,
  inspectionPageRoleLabel,
  inspectionReachabilityEvidence,
  inspectionReachabilityLabel,
  inspectionReplayEligibility,
  inspectionReplayEligibilityLabel,
  inspectionReportSummary,
  inspectionFallbackImageReady,
  inspectionLiveActionPanel,
  inspectionLiveCanvasMatchesPanel,
  inspectionLivePanelEpoch,
  inspectionLivePanelOwnerId,
  inspectionObservationOrdinal,
  inspectionPageDisplayName,
  INSPECTION_LIVE_SNAPSHOT_EVENT_TYPES,
  inspectionPhaseLabel,
  inspectionTerminalReviewState,
  isInspectionVerificationPhase,
  mergeInspectionLiveSnapshot,
  shouldClearInspectionActionOverlay,
} from '../src/utils/inspectionPresentation.js'

const state = (id, parent = null, incoming = null, branch = 'guest', overrides = {}) => ({
  id: String(id),
  state_id: id,
  branch_key: branch,
  parent_state_id: parent,
  incoming_transition_id: incoming,
  activity: `Activity${id}`,
  depth: parent === null ? 0 : 1,
  stable_status: 'STABLE',
  ...overrides,
})

const link = (id, source, target, sequence = id, status = 'PASS', actionType = 'click', overrides = {}) => ({
  id: String(id), source: String(source), target: target === null ? null : String(target), sequence, status,
  action_type: actionType, target_meta: { content_desc: `Action${id}` },
  ...overrides,
})

const flatten = root => {
  const result = []
  const visit = node => {
    result.push(node)
    node.children?.forEach(visit)
  }
  visit(root)
  return result
}

test('uses page nodes as the hierarchy and stores discovery actions on child edges', () => {
  const tree = buildInspectionMindMap({
    runId: 1,
    nodes: [state(1), state(2, 1, 10), state(3, 2, 11)],
    links: [link(10, 1, 2), link(11, 2, 3)],
  })
  assert.equal(tree.kind, 'virtual-root')
  assert.equal(tree.children[0].id, 'state-1')
  assert.equal(tree.children[0].children[0].id, 'state-2')
  assert.equal(tree.children[0].children[0].incomingTransition.id, '10')
  assert.equal(flatten(tree).some(item => item.kind === 'action-leaf'), false)
})

test('labels each business home first and names descendants from their entry action', () => {
  const tree = buildInspectionMindMap({
    runId: 1,
    nodes: [
      state(1),
      state(2, 1, 10),
      state(3, 2, 11),
      state(20, null, null, 'authenticated'),
    ],
    links: [link(10, 1, 2), link(11, 2, 3)],
  })
  const all = flatten(tree)
  assert.equal(all.find(item => item.id === 'state-1').name, '未登录首页 · P001')
  assert.equal(all.find(item => item.id === 'state-2').name, 'Action10 · P002')
  assert.equal(all.find(item => item.id === 'state-3').name, 'Action11 · P003')
  assert.equal(all.find(item => item.id === 'state-20').name, '已登录首页 · P004')
  assert.equal(tree.children.filter(item => item.payload?.tree_role === 'home').length, 2)
})

test('assigns stable task-local page labels before filtering', () => {
  const displayed = assignInspectionDisplayLabels([
    state(30, null, null, 'guest', { display_label: 'S1390' }),
    state(10),
    state(20),
  ])
  assert.deepEqual(
    displayed.map(item => [item.state_id, item.display_label]),
    [[30, 'P003'], [10, 'P001'], [20, 'P002']],
  )
  const filteredTree = buildInspectionMindMap({
    runId: 1,
    nodes: displayed.filter(item => item.state_id === 30),
    links: [],
  })
  assert.equal(filteredTree.children[0].name, '未登录首页 · P003')
})

test('uses real image proportions for portrait and landscape thumbnails', () => {
  assert.deepEqual(inspectionThumbnailSymbolSize({ image_width: 1080, image_height: 2400 }), [50.4, 112])
  assert.deepEqual(inspectionThumbnailSymbolSize({ image_width: 2400, image_height: 1350 }), [96, 54])
  assert.deepEqual(inspectionThumbnailSymbolSize({}, { width: 107, height: 240 }), [49.93, 112])
  assert.deepEqual(
    inspectionThumbnailSymbolSize({ image_width: 1080, image_height: 2400 }, { width: 1600, height: 900 }),
    [96, 54],
  )
})

test('turns Android implementation classes into user-facing action labels', () => {
  assert.equal(actionTargetLabel({
    action_type: 'click',
    target_meta: { class: 'android.view.ViewGroup' },
  }), '未命名页面入口')
  assert.equal(actionTargetLabel({
    action_type: 'scroll',
    target_meta: { class: 'android.widget.ScrollView' },
  }), '页面滚动')
  assert.equal(actionTargetLabel({
    action_type: 'click',
    coordinate_only: true,
    target_meta: { class: 'android.widget.ImageView' },
  }), '图片入口')
})

test('resolves every thumbnail source to an authenticated API request', () => {
  assert.deepEqual(
    inspectionThumbnailAssetRequest({ thumbnail_asset_id: 'sha256:abc' }, 47),
    { key: 'asset:sha256:abc', assetId: 'sha256:abc', path: null },
  )
  assert.deepEqual(
    inspectionThumbnailAssetRequest({ thumbnail_path: 'inspection/47/page/thumb.jpg' }, 47),
    {
      key: 'inspection:47:inspection/47/page/thumb.jpg',
      assetId: null,
      path: 'inspection/47/page/thumb.jpg',
    },
  )
  assert.deepEqual(
    inspectionThumbnailAssetRequest({
      thumbnail_url: '/api/inspections/runs/47/assets?path=inspection%2F47%2Fthumb.jpg',
    }, 47),
    {
      key: 'inspection:47:inspection/47/thumb.jpg',
      assetId: null,
      path: 'inspection/47/thumb.jpg',
    },
  )
  assert.deepEqual(
    inspectionThumbnailAssetRequest({ thumbnail_url: '/api/assets/sha256%3Adef' }, 47),
    { key: 'asset:sha256:def', assetId: 'sha256:def', path: null },
  )
  assert.equal(inspectionThumbnailAssetRequest({ thumbnail_url: 'https://example.com/public.jpg' }, 47), null)
})

test('fits, zooms, centers, and bounds a large page tree in one viewport coordinate system', () => {
  assert.deepEqual(fitMindMapViewport({
    viewportWidth: 1000,
    viewportHeight: 600,
    contentWidth: 4000,
    contentHeight: 2000,
    padding: 0,
  }), { scale: 0.25, scrollLeft: 0, scrollTop: 0 })

  assert.deepEqual(zoomMindMapViewport({
    scale: 0.5,
    factor: 2,
    scrollLeft: 250,
    scrollTop: 100,
    viewportWidth: 500,
    viewportHeight: 300,
    contentWidth: 2000,
    contentHeight: 1000,
  }), { scale: 1, scrollLeft: 750, scrollTop: 350 })
  assert.deepEqual(centerMindMapViewport({
    pointX: 1800,
    pointY: 900,
    scale: 1,
    viewportWidth: 500,
    viewportHeight: 300,
    contentWidth: 2000,
    contentHeight: 1000,
  }), { scrollLeft: 1500, scrollTop: 700 })
  assert.deepEqual(
    mindMapScrollOptions({ scrollLeft: 1500, scrollTop: 700 }, 'smooth'),
    { left: 1500, top: 700, behavior: 'smooth' },
  )

  const exportSize = boundedMindMapExportSize({ width: 14000, height: 32000, pixelRatio: 2 })
  assert.ok(exportSize.width <= 4096)
  assert.ok(exportSize.height <= 4096)
  assert.ok(exportSize.width * exportSize.height <= 12_000_000)

  const tree = {
    id: 'root',
    children: [
      { id: 'left', children: [{ id: 'left-leaf', children: [] }] },
      { id: 'right', children: [{ id: 'right-leaf', children: [] }] },
    ],
  }
  const positions = inspectionMindMapNodePositions(tree, { width: 1000, height: 600 })
  assert.equal(positions.get('root').x, 28)
  assert.equal(positions.get('left-leaf').x, 850)
  assert.ok(positions.get('left-leaf').y < positions.get('root').y)
  assert.ok(positions.get('right-leaf').y > positions.get('root').y)
})

test('opens a report at a readable scale and focuses failures on the last active page', () => {
  assert.equal(focusedMindMapScale({ viewportWidth: 390 }), 0.7)
  assert.equal(focusedMindMapScale({ viewportWidth: 800 }), 0.8)
  assert.equal(focusedMindMapScale({ viewportWidth: 1440 }), 1)

  assert.equal(inspectionMindMapInitialFocusId({
    status: 'PASS',
    lastActiveStateId: 19,
    homeStateId: 1,
  }), 'state-1')
  assert.equal(inspectionMindMapInitialFocusId({
    status: 'WARNING',
    terminalOutcome: 'SAFETY_BLOCKED',
    lastActiveStateId: 19,
    homeStateId: 1,
  }), 'state-1')
  assert.equal(inspectionMindMapInitialFocusId({
    status: 'ABORTED',
    lastActiveStateId: 19,
    currentStateId: 18,
    homeStateId: 1,
  }), 'state-19')
  assert.equal(inspectionMindMapInitialFocusId({
    status: 'WARNING',
    terminalOutcome: 'APP_FAULT',
    lastActiveStateId: 17,
    homeStateId: 1,
  }), 'state-17')
  assert.equal(inspectionMindMapInitialFocusId({
    status: 'RUNNING',
    lastActiveStateId: 7,
    currentStateId: 8,
    homeStateId: 1,
  }), 'state-8')
})

test('key-path mode only groups equivalent siblings and preserves every downstream page', () => {
  const familyPage = {
    exploration_family_id: 7,
    page_subtype: 'CATALOG_CATEGORY',
    page_role: 'LIST',
    terminal_boundaries: [{ terminal_outcome: 'NONE' }],
  }
  const base = buildInspectionMindMap({
    runId: 1,
    nodes: [
      state(1),
      state(2, 1, 10, 'guest', familyPage),
      state(3, 1, 11, 'guest', familyPage),
      state(4, 2, 12, 'guest', { depth: 2 }),
      state(5, 3, 13, 'guest', { depth: 2 }),
    ],
    links: [link(10, 1, 2), link(11, 1, 3), link(12, 2, 4), link(13, 3, 5)],
  })
  const keyTree = aggregateInspectionMindMap(base, 'key')
  const group = keyTree.children[0].children[0]
  assert.equal(keyTree.children[0].children.length, 1)
  assert.match(group.name, /同类页面 \+1$/)
  assert.deepEqual(group.payload.aggregated_state_ids, [2, 3])
  assert.deepEqual(group.children.map(item => item.payload.state_id), [4, 5])
  assert.equal(aggregateInspectionMindMap(base, 'all'), base)
})

test('key-path aggregation ignores volatile ids and reasons but preserves boundary semantics', () => {
  const node = overrides => ({
    kind: 'state',
    payload: {
      tree_role: 'page',
      exploration_family_id: 7,
      page_subtype: 'CATALOG_CATEGORY',
      page_role: 'LIST',
      terminal_boundaries: [{
        transition_id: 101,
        boundary_id: 'transition-101',
        reason: 'first wording',
        terminal_outcome: 'SAFETY_BLOCKED',
        risk_type: 'PAYMENT',
        action_role: 'PAYMENT:SUBMIT',
        boundary_evidence: 'VERIFIED',
        attention_required: false,
      }],
      ...overrides,
    },
  })
  const first = inspectionMindMapAggregationSignature(node())
  const second = inspectionMindMapAggregationSignature(node({
    terminal_boundaries: [{
      transition_id: 999,
      boundary_id: 'transition-999',
      reason: 'wording changed',
      terminal_outcome: 'SAFETY_BLOCKED',
      risk_type: 'PAYMENT',
      action_role: 'PAYMENT:SUBMIT',
      boundary_evidence: 'VERIFIED',
      attention_required: false,
    }],
  }))
  assert.deepEqual(first, second)
  assert.equal(JSON.stringify(first).includes('transition'), false)
  assert.equal(JSON.stringify(first).includes('wording'), false)

  for (const terminalBoundary of [
    { terminal_outcome: 'APP_FAULT', risk_type: 'PAYMENT', action_role: 'PAYMENT:SUBMIT', boundary_evidence: 'VERIFIED' },
    { terminal_outcome: 'SAFETY_BLOCKED', risk_type: 'DESTRUCTIVE', action_role: 'PAYMENT:SUBMIT', boundary_evidence: 'VERIFIED' },
    { terminal_outcome: 'SAFETY_BLOCKED', risk_type: 'PAYMENT', action_role: 'BUY_NOW', boundary_evidence: 'VERIFIED' },
    { terminal_outcome: 'SAFETY_BLOCKED', risk_type: 'PAYMENT', action_role: 'PAYMENT:SUBMIT', boundary_evidence: 'CHANGED' },
  ]) {
    assert.notDeepEqual(first, inspectionMindMapAggregationSignature(node({
      terminal_boundaries: [terminalBoundary],
    })))
  }

  assert.equal(inspectionMindMapNodeNeedsAttention({
    attention_required: true,
    reachability_evidence: 'VERIFIED_TWICE',
    replay_scope: 'FULL_PATH',
  }), true)
  assert.equal(inspectionMindMapNodeNeedsAttention({
    terminal_boundaries: [{ attention_required: true }],
    reachability_evidence: 'VERIFIED_TWICE',
    replay_scope: 'FULL_PATH',
  }), true)
  assert.equal(inspectionMindMapNodeNeedsAttention({
    reachability_evidence: 'VERIFIED_TWICE',
    replay_scope: 'FULL_PATH',
  }), false)
})

test('keeps a cross edge and self loop as finite references', () => {
  const tree = buildInspectionMindMap({
    runId: 1,
    nodes: [state(1), state(2, 1, 10), state(3, 2, 11)],
    links: [link(10, 1, 2), link(11, 2, 3), link(12, 1, 3), link(13, 3, 3, 4, 'SELF_LOOP')],
  })
  const all = flatten(tree)
  assert.equal(all.filter(item => item.id === 'state-3').length, 1)
  assert.equal(all.filter(item => item.kind === 'reference' && item.payload.state_id === 3).length, 2)
})

test('keeps blocked and targetless actions in page details without crowding the tree', () => {
  const blocked = link(10, 1, null, 1, 'BLOCKED')
  const tree = buildInspectionMindMap({ runId: 1, nodes: [state(1)], links: [blocked] })
  const home = tree.children[0]
  assert.equal(flatten(tree).some(item => item.kind === 'action-leaf'), false)
  assert.equal(home.payload.non_navigation_actions.length, 1)
  assert.equal(home.payload.non_navigation_actions[0].status, 'BLOCKED')
})

test('keeps a VIEWPORT capture as a same-page leaf and promotes its clicked page', () => {
  const viewport = state(2, 1, 10, 'guest', { depth: 0, stable_status: 'VIEWPORT' })
  const destination = state(3, 2, 11, 'guest', { depth: 1 })
  const tree = buildInspectionMindMap({
    runId: 1,
    nodes: [state(1), viewport, destination],
    links: [
      link(10, 1, 2, 1, 'PASS', 'scroll'),
      link(11, 2, 3, 2, 'PASS', 'click'),
    ],
  })

  const home = tree.children[0]
  const viewportNode = home.children.find(item => item.id === 'state-2')
  const destinationNode = home.children.find(item => item.id === 'state-3')
  assert.equal(viewportNode.payload.tree_role, 'viewport')
  assert.equal(viewportNode.payload.viewport_auxiliary, true)
  assert.equal(viewportNode.level, home.level)
  assert.equal(viewportNode.name, '同页视口 · P002')
  assert.deepEqual(viewportNode.children, [])
  assert.equal(viewportNode.incomingTransition.id, '10')
  assert.equal(destinationNode.level, home.level + 1)
  assert.equal(destinationNode.incomingTransition.id, '11')
  assert.equal(destinationNode.payload.parent_state_id, 2)
})

test('treats an uncommitted same-depth scroll target as a viewport', () => {
  const pollingViewport = state(2, 1, 10, 'guest', { depth: 0, stable_status: 'UNVERIFIED' })
  const destination = state(3, 2, 11, 'guest', { depth: 1 })
  const tree = buildInspectionMindMap({
    runId: 1,
    nodes: [state(1), pollingViewport, destination],
    links: [
      link(10, 1, 2, 1, 'PASS', 'scroll'),
      link(11, 2, 3, 2),
    ],
  })

  const home = tree.children[0]
  assert.equal(home.children.find(item => item.id === 'state-2').payload.tree_role, 'viewport')
  assert.ok(home.children.some(item => item.id === 'state-3'))
  assert.equal(home.children.find(item => item.id === 'state-3').name, 'Action11 · P003')
})

test('does not flatten a scroll target when its business depth changes', () => {
  const destination = state(2, 1, 10, 'guest', { depth: 1, stable_status: 'STABLE' })
  const child = state(3, 2, 11, 'guest', { depth: 2 })
  const tree = buildInspectionMindMap({
    runId: 1,
    nodes: [state(1), destination, child],
    links: [
      link(10, 1, 2, 1, 'PASS', 'scroll'),
      link(11, 2, 3, 2),
    ],
  })

  const page2 = tree.children[0].children.find(item => item.id === 'state-2')
  assert.equal(page2.payload.tree_role, 'page')
  assert.ok(page2.children.some(item => item.id === 'state-3'))
})

test('flattens chained viewports, retains their actions, and keeps loops finite', () => {
  const firstViewport = state(2, 1, 10, 'guest', { depth: 0, stable_status: 'VIEWPORT' })
  const secondViewport = state(3, 2, 11, 'guest', { depth: 0, stable_status: 'VIEWPORT' })
  const destination = state(4, 3, 12, 'guest', { depth: 1 })
  const tree = buildInspectionMindMap({
    runId: 1,
    nodes: [state(1), firstViewport, secondViewport, destination],
    links: [
      link(10, 1, 2, 1, 'PASS', 'scroll'),
      link(11, 2, 3, 2, 'PASS', 'scroll'),
      link(12, 3, 4, 3),
      link(13, 3, null, 4, 'BLOCKED'),
      link(14, 3, 2, 5, 'SELF_LOOP', 'scroll'),
    ],
  })

  const home = tree.children[0]
  assert.ok(home.children.some(item => item.id === 'state-2' && item.payload.tree_role === 'viewport'))
  assert.ok(home.children.some(item => item.id === 'state-3' && item.payload.tree_role === 'viewport'))
  assert.ok(home.children.some(item => item.id === 'state-4'))
  assert.equal(
    home.children.find(item => item.id === 'state-3').payload.non_navigation_actions[0].id,
    '13',
  )
  assert.ok(home.children.some(item => item.kind === 'reference' && item.payload.state_id === 2))
  assert.equal(flatten(tree).filter(item => item.kind === 'state').length, 4)
})

test('uses the earliest valid incoming edge while fields are absent during polling', () => {
  const child = state(2)
  delete child.parent_state_id
  delete child.incoming_transition_id
  const tree = buildInspectionMindMap({
    runId: 1,
    nodes: [state(1), child],
    links: [link(11, 1, 2, 2), link(10, 1, 2, 1)],
  })
  const page2 = flatten(tree).find(item => item.id === 'state-2')
  const reference = flatten(tree).find(item => item.kind === 'reference')
  assert.equal(page2.incomingTransition.id, '10')
  assert.equal(reference.payload.transition.id, '11')
})

test('places a state with a missing parent directly in the first level as unassociated', () => {
  const tree = buildInspectionMindMap({ runId: 1, nodes: [state(2, 99, 10)], links: [] })
  assert.equal(tree.children[0].id, 'state-2')
  assert.equal(tree.children[0].payload.orphan, true)
  assert.match(tree.children[0].name, /^未关联/)
})

test('keeps version 1 reports on the legacy tree even when hierarchy fields are present', () => {
  const tree = buildInspectionMindMap({
    runId: 1,
    hierarchyVersion: 1,
    nodes: [
      state(1, null, null, 'guest', { hierarchy_role: 'BRANCH_ROOT' }),
      state(2, 1, 10, 'guest', { hierarchy_role: 'PEER', depth: 1 }),
    ],
    links: [
      link(10, 1, 2, 1, 'PASS', 'click', { relation_type: 'PEER' }),
    ],
  })

  const home = tree.children[0]
  assert.equal(home.id, 'state-1')
  assert.equal(home.children[0].id, 'state-2')
  assert.equal(home.children[0].level, 1)
  assert.equal(home.children[0].payload.tree_role, 'page')
})

test('renders persisted root-level PEER tabs beside the branch root without duplicate references', () => {
  const tree = buildInspectionMindMap({
    runId: 1,
    hierarchyVersion: 2,
    nodes: [
      state(1, null, 12, 'guest', { hierarchy_role: 'BRANCH_ROOT' }),
      state(2, null, 10, 'guest', { hierarchy_role: 'PEER' }),
      state(3, null, 11, 'guest', { hierarchy_role: 'PEER' }),
    ],
    links: [
      link(10, 1, 2, 1, 'PASS', 'click', { relation_type: 'PEER', relation_confidence: 0.96 }),
      link(11, 2, 3, 2, 'PASS', 'click', { relation_type: 'PEER', relation_confidence: 0.94 }),
      link(12, 3, 1, 3, 'PASS', 'click', { relation_type: 'PEER', relation_confidence: 0.97 }),
    ],
  })

  assert.deepEqual(tree.children.map(item => item.id), ['state-1', 'state-2', 'state-3'])
  assert.deepEqual(tree.children.map(item => item.level), [0, 0, 0])
  assert.equal(tree.children[0].payload.tree_role, 'home')
  assert.equal(tree.children[0].name, '未登录首页 · P001')
  assert.equal(tree.children[0].incomingTransition, null)
  assert.equal(tree.children[1].payload.tree_role, 'peer')
  assert.equal(tree.children[1].name, 'Action10 · P002')
  assert.equal(tree.children[2].name, 'Action11 · P003')
  assert.equal(flatten(tree).some(item => item.kind === 'reference'), false)
})

test('does not relabel a PEER tab as home when filtering hides BRANCH_ROOT', () => {
  const tree = buildInspectionMindMap({
    runId: 1,
    hierarchyVersion: 2,
    nodes: [state(2, null, 10, 'guest', { hierarchy_role: 'PEER' })],
    links: [],
  })

  assert.equal(tree.children[0].payload.tree_role, 'peer')
  assert.notEqual(tree.children[0].name, '未登录首页 · P002')
})

test('uses parent_state_id for nested peers even when their incoming source is a sibling', () => {
  const tree = buildInspectionMindMap({
    runId: 1,
    hierarchyVersion: 2,
    nodes: [
      state(1, null, null, 'guest', { hierarchy_role: 'BRANCH_ROOT' }),
      state(2, 1, 10, 'guest', { hierarchy_role: 'PAGE', depth: 1 }),
      state(3, 1, 11, 'guest', { hierarchy_role: 'PEER', depth: 1 }),
    ],
    links: [
      link(10, 1, 2, 1, 'PASS', 'click', { relation_type: 'CHILD' }),
      link(11, 2, 3, 2, 'PASS', 'click', { relation_type: 'PEER' }),
    ],
  })

  const home = tree.children[0]
  assert.deepEqual(home.children.map(item => item.id), ['state-2', 'state-3'])
  assert.deepEqual(home.children.map(item => item.level), [1, 1])
  assert.equal(home.children[1].incomingTransition.id, '11')
  assert.equal(home.children[1].name, 'Action11 · P003')
  assert.equal(home.children[0].children.some(item => item.id === 'state-3'), false)
  assert.equal(flatten(tree).some(item => item.kind === 'reference'), false)
})

test('keeps non-PEER cross-page transitions as references with persisted hierarchy', () => {
  const tree = buildInspectionMindMap({
    runId: 1,
    hierarchyVersion: 2,
    nodes: [
      state(1, null, null, 'guest', { hierarchy_role: 'BRANCH_ROOT' }),
      state(2, 1, 10, 'guest', { hierarchy_role: 'PAGE', depth: 1 }),
      state(3, 1, 11, 'guest', { hierarchy_role: 'PAGE', depth: 1 }),
    ],
    links: [
      link(10, 1, 2, 1, 'PASS', 'click', { relation_type: 'CHILD' }),
      link(11, 1, 3, 2, 'PASS', 'click', { relation_type: 'CHILD' }),
      link(12, 2, 3, 3, 'PASS', 'click', { relation_type: 'CHILD' }),
      link(13, 3, 3, 4, 'SELF_LOOP', 'click', { relation_type: 'SELF' }),
    ],
  })

  const references = flatten(tree).filter(item => item.kind === 'reference')
  assert.equal(references.length, 2)
  assert.deepEqual(references.map(item => item.payload.transition.id), ['12', '13'])
})

test('flattens persisted VIEWPORT hierarchy while preserving its discovered page', () => {
  const tree = buildInspectionMindMap({
    runId: 1,
    hierarchyVersion: 2,
    nodes: [
      state(1, null, null, 'guest', { hierarchy_role: 'BRANCH_ROOT' }),
      state(2, 1, 10, 'guest', { hierarchy_role: 'VIEWPORT', depth: 0, stable_status: 'VIEWPORT' }),
      state(3, 2, 11, 'guest', { hierarchy_role: 'PAGE', depth: 1 }),
    ],
    links: [
      link(10, 1, 2, 1, 'PASS', 'scroll', { relation_type: 'VIEWPORT' }),
      link(11, 2, 3, 2, 'PASS', 'click', { relation_type: 'CHILD' }),
    ],
  })

  const home = tree.children[0]
  assert.deepEqual(home.children.map(item => item.id), ['state-2', 'state-3'])
  assert.equal(home.children[0].payload.tree_role, 'viewport')
  assert.equal(home.children[0].level, 0)
  assert.equal(home.children[1].payload.tree_role, 'page')
  assert.equal(home.children[1].level, 1)
  assert.equal(home.children[1].incomingTransition.id, '11')
})

test('preserves collapse preferences and initially expands roots only', () => {
  const base = buildInspectionMindMap({
    runId: 1,
    nodes: [state(1), state(2, 1, 10), state(3, 2, 11)],
    links: [link(10, 1, 2), link(11, 2, 3)],
  })
  const initial = applyMindMapCollapseState(base)
  assert.equal(flatten(initial).find(item => item.id === 'state-1').folded, false)
  assert.equal(flatten(initial).find(item => item.id === 'state-2').folded, true)
  assert.equal(flatten(initial).find(item => item.id === 'state-3'), undefined)
  const placeholder = flatten(initial).find(item => item.kind === 'collapse-placeholder')
  assert.equal(placeholder.payload.owner_id, 'state-2')
  assert.equal(placeholder.payload.child_count, 1)
  assert.equal(placeholder.payload.hidden_page_count, 1)
  assert.equal(placeholder.name, '已收起 1 个页面')
  assert.deepEqual(placeholder.lineStyle, PAGE_TREE_LINE_STYLE)
  const updated = applyMindMapCollapseState(base, new Map([['state-2', false]]))
  assert.equal(flatten(updated).find(item => item.id === 'state-2').folded, false)
  assert.ok(flatten(updated).find(item => item.id === 'state-3'))
  assert.ok(collectExpandableMindMapNodes(base).some(item => item.id === 'state-2'))
})

test('collapsed placeholders count every hidden page and all-page mode remains initially bounded', () => {
  const base = buildInspectionMindMap({
    runId: 1,
    nodes: [
      state(1),
      state(2, 1, 10),
      state(3, 2, 11, 'guest', { depth: 2 }),
      state(4, 3, 12, 'guest', { depth: 3 }),
    ],
    links: [link(10, 1, 2), link(11, 2, 3), link(12, 3, 4)],
  })
  const allPages = applyMindMapCollapseState(aggregateInspectionMindMap(base, 'all'))
  const placeholder = flatten(allPages).find(item => item.payload?.owner_id === 'state-2')
  assert.equal(placeholder.payload.hidden_page_count, 2)
  assert.equal(placeholder.name, '已收起 2 个页面')
  assert.equal(flatten(allPages).some(item => item.id === 'state-3'), false)
  assert.equal(flatten(allPages).some(item => item.id === 'state-4'), false)
})

test('keeps explicit collapse choices after a polling refresh creates a new tree', () => {
  const input = {
    runId: 1,
    nodes: [state(1), state(2, 1, 10), state(3, 2, 11)],
    links: [link(10, 1, 2), link(11, 2, 3)],
  }
  const preferences = new Map([['state-2', false]])
  const first = applyMindMapCollapseState(buildInspectionMindMap(input), preferences)
  const refreshed = applyMindMapCollapseState(buildInspectionMindMap({
    ...input,
    nodes: input.nodes.map(item => ({ ...item })),
    links: input.links.map(item => ({ ...item })),
  }), preferences)
  assert.equal(flatten(first).find(item => item.id === 'state-2').folded, false)
  assert.equal(flatten(refreshed).find(item => item.id === 'state-2').folded, false)
})

test('uses one neutral solid connector style for every rendered relationship', () => {
  const tree = buildInspectionMindMap({
    runId: 1,
    nodes: [
      state(1),
      state(2, 1, 10),
      state(3, 2, 11),
      state(4, 1, 13, 'guest', { depth: 0, stable_status: 'VIEWPORT' }),
      state(20, null, null, 'authenticated'),
      state(30, 99, 99, 'orphaned'),
    ],
    links: [
      link(10, 1, 2, 1, 'PASS'),
      link(11, 2, 3, 2, 'ERROR'),
      link(12, 1, 3, 3, 'BLOCKED'),
      link(13, 1, 4, 4, 'PASS', 'scroll'),
    ],
  })
  const relationshipNodes = flatten(tree).filter(item => ['state', 'reference'].includes(item.kind))
  assert.ok(relationshipNodes.some(item => item.payload?.tree_role === 'home'))
  assert.ok(relationshipNodes.some(item => item.payload?.tree_role === 'viewport'))
  assert.ok(relationshipNodes.some(item => item.payload?.tree_role === 'orphan'))
  relationshipNodes.forEach(item => assert.deepEqual(item.lineStyle, PAGE_TREE_LINE_STYLE))
})

test('keeps an 800-action cyclic report finite', () => {
  const links = Array.from({ length: 800 }, (_, index) => link(index + 1, 1, 1, index + 1, 'SELF_LOOP'))
  const tree = buildInspectionMindMap({ runId: 1, nodes: [state(1)], links })
  const all = flatten(tree)
  assert.equal(all.filter(item => item.kind === 'reference').length, 800)
  assert.equal(all.filter(item => item.kind === 'state').length, 1)
})

test('uses graph v3 topology for finite loop labels independently of status', () => {
  const tree = buildInspectionMindMap({
    runId: 1,
    hierarchyVersion: 2,
    nodes: [
      state(1, null, null, 'guest', { hierarchy_role: 'BRANCH_ROOT' }),
      state(2, 1, 10, 'guest', { hierarchy_role: 'PAGE' }),
    ],
    links: [
      link(10, 1, 2, 1, 'PASS', 'click', { topology_type: 'TREE' }),
      link(11, 2, 1, 2, 'PASS', 'click', {
        topology_type: 'CYCLE_BACK',
        traversal_count: 7,
      }),
      link(12, 2, 2, 3, 'PASS', 'click', {
        topology_type: 'SELF_LOOP',
        traversal_count: 3,
      }),
    ],
  })
  const references = flatten(tree).filter(item => item.kind === 'reference')
  assert.deepEqual(references.map(item => item.name), [
    '回到 P001 · 7 次',
    '回到 P002 · 3 次',
  ])
  assert.equal(flatten(tree).filter(item => item.kind === 'state').length, 2)
})

test('labels v4 family representatives and delta states without changing their identity', () => {
  const tree = buildInspectionMindMap({
    runId: 1,
    nodes: [
      state(1, null, null, 'guest', { exploration_mode: 'FULL', exploration_family_id: 7 }),
      state(2, 1, 10, 'guest', { exploration_mode: 'DELTA_ONLY', exploration_family_id: 7 }),
    ],
    links: [link(10, 1, 2)],
  })
  const all = flatten(tree)
  assert.equal(all.find(item => item.id === 'state-1').name, '未登录首页 · P001 · 代表')
  assert.equal(all.find(item => item.id === 'state-2').name, 'Action10 · P002 · 增量')
  assert.equal(all.filter(item => item.kind === 'state').length, 2)
})

test('presents v4 action failure categories and verification phase without breaking legacy statuses', () => {
  assert.equal(inspectionActionStatus({ status: 'NOT_REACHED' }), 'NOT_REACHED')
  assert.equal(inspectionActionStatusMeta({ status: 'NOT_REACHED' }).label, '收尾未执行（历史）')
  assert.equal(inspectionActionStatus({ status: 'NOT_REACHED', failure_type: 'PARENT_RECOVERY_FAILED' }), 'PARENT_RECOVERY_FAILED')
  assert.equal(inspectionActionStatusMeta('COORDINATE_STALE').label, '坐标已过期')
  assert.equal(inspectionActionStatusMeta('PARENT_RECOVERY_CASCADE').label, '父页恢复失败（批次）')
  assert.equal(inspectionActionStatusMeta('BUDGET_LIMIT').label, '达到预算上限')
  assert.equal(inspectionExecutionDispositionLabel('FAMILY_REUSED'), '同构族复用')
  assert.equal(inspectionExecutionDispositionLabel('CONTRACT_REUSED'), '覆盖契约复用')
  assert.equal(inspectionActionStatusMeta('SAMPLED_OUT').label, '代表采样跳过')
  assert.equal(inspectionActionStatusMeta('NAVIGATION_REUSED').label, '导航已复用')
  assert.equal(inspectionActionStatusMeta('VISUAL_STALE').label, '视觉入口已变化')
  assert.equal(inspectionExecutionDispositionLabel('RESULT_UNKNOWN'), '结果未知')
  assert.equal(inspectionPhaseLabel('STABLE_PATH_VERIFICATION'), '验证稳定路径')
  assert.equal(inspectionPhaseLabel('recover_parent'), '恢复页面')
  assert.equal(inspectionPhaseLabel('entry_survey'), '入口普查')
  assert.equal(inspectionPhaseLabel('coverage_explore'), '覆盖探索')
  assert.equal(inspectionPhaseLabel('representative_verification'), '代表验证')
  assert.equal(isInspectionVerificationPhase('STABLE_PATH_VERIFICATION'), true)
  assert.equal(isInspectionVerificationPhase('EXPLORATION'), false)
  assert.equal(isInspectionVerificationPhase('REPRESENTATIVE_VERIFICATION'), true)
  assert.equal(shouldClearInspectionActionOverlay('explore', '验证稳定路径'), true)
  assert.equal(shouldClearInspectionActionOverlay('recover', '父页面恢复'), false)
  ;['PHASE_CHANGED', 'FRONTIER_UPDATED', 'ACTION_DEFERRED', 'ACTION_RESUMED', 'ACTION_COVERED_BY_FAMILY'].forEach(eventType => {
    assert.equal(INSPECTION_LIVE_SNAPSHOT_EVENT_TYPES.has(eventType), true)
  })
  ;['ACTION_COVERED_BY_CONTRACT', 'ACTION_NAVIGATION_REUSED', 'ACTION_SAMPLED_OUT'].forEach(eventType => {
    assert.equal(INSPECTION_LIVE_SNAPSHOT_EVENT_TYPES.has(eventType), true)
  })
})

test('presents reachability separately from safe replay and summarizes four report outcomes', () => {
  const observed = {
    observation_count: 1,
    stable_status: 'UNVERIFIED',
    locator_quality: 'RESOURCE_ID',
    replay_eligibility: 'SAFE_PREFIX',
  }
  const verified = {
    observation_count: 2,
    stable_status: 'STABLE',
    locator_quality: 'TEXT',
    replay_eligibility: 'FULL',
  }
  assert.equal(inspectionReachabilityEvidence(observed), 'OBSERVED_ONCE')
  assert.equal(inspectionReachabilityEvidence(verified), 'OBSERVED_ONCE')
  assert.equal(inspectionReachabilityEvidence({ ...verified, stable_status: 'VERIFIED_TWICE' }), 'VERIFIED_TWICE')
  assert.equal(inspectionReachabilityLabel('OBSERVED_ONCE'), '已到达，待复验')
  assert.equal(inspectionReplayEligibility(observed, []), 'SAFE_PREFIX')
  assert.equal(inspectionReplayEligibilityLabel('SAFE_PREFIX'), '可安全回放前缀')
  assert.equal(inspectionPageRoleLabel('PRODUCT_DETAIL'), '商品详情')
  assert.equal(inspectionPageRoleLabel('CASHIER'), '收银台')
  assert.equal(inspectionReplayEligibility(
    { observation_count: 1, stable_status: 'STABLE' },
    [{ status: 'PASS', risk_type: 'PAYMENT' }],
  ), 'FULL')

  const summary = inspectionReportSummary({
    graph: {
      stats: {
        families_discovered: 4,
        family_representatives_expanded: 3,
        family_coverage_ratio: 0.75,
      },
    },
    run: { fault_count: 2 },
    nodes: [observed, verified],
  })
  assert.deepEqual(summary.family, { total: 4, expanded: 3, ratio: 0.75 })
  assert.equal(summary.reached, 2)
  assert.deepEqual(summary.replay, {
    total: 2,
    full: 1,
    safePrefix: 1,
    verified: 0,
    observed: 0,
    diagnosticOnly: 0,
    candidateCount: 2,
    defaultSelectionLimit: 0,
  })
  assert.equal(summary.faults, 2)
  assert.equal(summary.attention, 2)
  assert.deepEqual(summary.issues, {
    application: 2,
    infrastructure: 0,
    automation: 0,
  })
  assert.equal(summary.summaryAvailable, true)
})

test('presents v7 replay scopes and plain-language capture details with v6 fallback', () => {
  assert.equal(inspectionReplayEligibility({ replay_scope: 'FULL_PATH' }), 'FULL')
  assert.equal(inspectionReplayEligibility({ replay_scope: 'PREFIX_TO_SAFETY_BOUNDARY' }), 'SAFE_PREFIX')
  assert.equal(inspectionReplayEligibility({ replay_scope: 'DIAGNOSTIC_ONLY' }), 'NONE')
  assert.equal(inspectionReplayEligibilityLabel('FULL_PATH'), '可完整回放')
  assert.equal(inspectionReplayEligibilityLabel('PREFIX_TO_SAFETY_BOUNDARY'), '可安全回放前缀')
  assert.equal(inspectionCaptureKindLabel('DISCOVERY'), '首次到达')
  assert.equal(inspectionCaptureKindLabel('VERIFICATION'), '路径复验')
  assert.equal(inspectionAssetAvailabilityLabel({ screenshot_asset_id: 's', xml_asset_id: 'x' }), '截图和页面结构可查看')
  assert.equal(inspectionObservationOrdinal({ total: 12, page: 1, pageSize: 10, index: 0 }), 12)
  assert.equal(inspectionObservationOrdinal({ total: 12, page: 2, pageSize: 10, index: 1 }), 1)
  assert.equal(inspectionPageDisplayName({ display_label: 'P08', page_title: '商品详情' }), 'P008 · 商品详情')
  assert.equal(inspectionPageDisplayName({}, 'P08 · 商品详情'), 'P008 · 商品详情')
  assert.equal(inspectionPageDisplayName({ page_title: '商品详情' }, 'P08 · 商品详情'), 'P008 · 商品详情')
  assert.equal(inspectionPageDisplayName({ display_label: 'S1390', display_index: 1, page_title: '商品详情' }), 'P001 · 商品详情')

  const summary = inspectionReportSummary({
    graph: {
      summary: {
        replay_paths: {
          total: 8,
          full_path: 5,
          safe_prefix: 3,
          diagnostic_only: 2,
          candidate_count: 7,
          default_selection_limit: 4,
        },
      },
    },
  })
  assert.deepEqual(summary.replay, {
    total: 8,
    full: 5,
    safePrefix: 3,
    verified: 0,
    observed: 0,
    diagnosticOnly: 2,
    candidateCount: 7,
    defaultSelectionLimit: 4,
  })
  assert.equal(inspectionReportSummary({ graph: { summary_available: false } }).summaryAvailable, false)
})

test('selects the terminal review page from explicit activity or observation time, never max database id', () => {
  const nodes = [
    { state_id: 999, last_observed_at: '2026-07-22T09:00:00Z' },
    { state_id: 12, representative_observation_id: 88, last_observed_at: '2026-07-22T10:00:00Z' },
    { state_id: 14, last_observed_at: '2026-07-22T11:00:00Z' },
  ]
  assert.equal(inspectionTerminalReviewState(nodes, { last_active_state_id: 12 }).state_id, 12)
  assert.equal(inspectionTerminalReviewState(nodes, { last_observation_id: 88 }).state_id, 12)
  assert.equal(inspectionTerminalReviewState(nodes, {}).state_id, 14)
})

test('keeps the active action-panel owner while a child is only observed or queued', () => {
  const parentSnapshot = {
    revision: 20,
    expansion_owner_state_id: 101,
    expansion_epoch: 4,
    page: { state_id: 101, activity: 'ProductList' },
    actions: [{ action_key: 'open-product', label: '打开商品' }],
    action_panel: {
      state_id: 101,
      expansion_epoch: 4,
      page: { state_id: 101, activity: 'ProductList' },
      actions: [{ action_key: 'open-product', label: '打开商品' }],
      current_action: { action_key: 'open-product', status: 'ACTIVE' },
      canvas_matches_panel: false,
    },
    // Compatibility fields may briefly describe another physical page on an
    // older mixed-version server. The explicit panel remains authoritative.
    device_context: { state_id: 202, canvas_matches_panel: false },
  }

  assert.equal(inspectionLivePanelOwnerId(parentSnapshot), 101)
  assert.equal(inspectionLivePanelEpoch(parentSnapshot), 4)
  assert.equal(inspectionLiveActionPanel(parentSnapshot).page.activity, 'ProductList')
  assert.equal(inspectionLiveActionPanel(parentSnapshot).actions[0].action_key, 'open-product')
  assert.equal(inspectionLiveCanvasMatchesPanel(parentSnapshot), false)

  const stalePoll = mergeInspectionLiveSnapshot(parentSnapshot, {
    revision: 19,
    expansion_owner_state_id: 202,
    page: { state_id: 202 },
  })
  assert.equal(stalePoll, parentSnapshot)
  assert.equal(inspectionLivePanelOwnerId(stalePoll), 101)

  const nextOwner = mergeInspectionLiveSnapshot(parentSnapshot, {
    revision: 21,
    expansion_owner_state_id: 202,
    expansion_epoch: 5,
    action_panel: {
      state_id: 202,
      expansion_epoch: 5,
      page: { state_id: 202, activity: 'ProductDetail' },
      actions: [{ action_key: 'checkout' }],
      canvas_matches_panel: true,
    },
  })
  assert.equal(inspectionLivePanelOwnerId(nextOwner), 202)
  assert.equal(inspectionLivePanelEpoch(nextOwner), 5)
  assert.equal(inspectionLiveCanvasMatchesPanel(nextOwner), true)

  const legacy = {
    page: { state_id: 7 },
    actions: [{ action_key: 'legacy' }],
  }
  assert.equal(inspectionLivePanelOwnerId(legacy), 7)
  assert.equal(inspectionLiveCanvasMatchesPanel(legacy), true)

  assert.equal(
    inspectionFallbackImageReady(
      'blob:parent',
      'inspection/1/101/screenshot.png',
      'inspection/1/101/screenshot.png',
    ),
    true,
  )
  assert.equal(
    inspectionFallbackImageReady(
      'blob:parent',
      'inspection/1/101/screenshot.png',
      'inspection/1/202/screenshot.png',
    ),
    false,
  )
  assert.equal(
    inspectionFallbackImageReady('', 'inspection/1/202/screenshot.png', 'inspection/1/202/screenshot.png'),
    false,
  )
})

test('orders live snapshots by stream incarnation before revision', () => {
  const streamA = {
    run_id: 19,
    stream_id: 'stream-a',
    stream_started_at: '2026-07-21T10:00:00.000Z',
    revision: 57,
    expansion_owner_state_id: 101,
    action_panel: { state_id: 101 },
  }
  const streamB = {
    run_id: 19,
    stream_id: 'stream-b',
    stream_started_at: '2026-07-21T10:03:00.000Z',
    revision: 1,
    expansion_owner_state_id: 202,
    action_panel: { state_id: 202 },
  }

  const restarted = mergeInspectionLiveSnapshot(streamA, streamB)
  assert.equal(restarted.stream_id, 'stream-b')
  assert.equal(restarted.revision, 1)
  assert.equal(restarted.expansion_owner_state_id, 202)
  assert.equal('actions' in restarted, false)

  const delayedOldStream = mergeInspectionLiveSnapshot(restarted, {
    ...streamA,
    revision: 58,
  })
  assert.equal(delayedOldStream, restarted)

  const staleCurrentStream = mergeInspectionLiveSnapshot(restarted, {
    ...streamB,
    revision: 0,
  })
  assert.equal(staleCurrentStream, restarted)

  const legacy = mergeInspectionLiveSnapshot(
    { run_id: 19, revision: 4, current_stage: '旧阶段' },
    { run_id: 19, revision: 5, current_stage: '新阶段' },
  )
  assert.equal(legacy.current_stage, '新阶段')
  assert.equal(legacy.revision, 5)

  const anotherRun = mergeInspectionLiveSnapshot(
    { run_id: 19, revision: 100, actions: [{ action_key: 'stale' }] },
    { run_id: 20, revision: 0, current_stage: '新任务' },
  )
  assert.deepEqual(anotherRun, {
    run_id: 20,
    revision: 0,
    current_stage: '新任务',
  })
})
