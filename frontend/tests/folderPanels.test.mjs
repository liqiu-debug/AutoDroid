import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { ref, computed, watch, effectScope } from 'vue'
import * as utilities from '../src/utils/apiTesting.js'

const read = path => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')
const folderTreePanel = read('components/FolderTreePanel.vue')
const assetListSource = read('views/api-testing/AssetList.vue')
const caseList = read('views/cases/CaseList.vue')
const scenarioList = read('views/scenarios/ScenarioList.vue')

async function flush() { for (let i = 0; i < 12; i++) await Promise.resolve() }

// 与 apiAssetRuns.test.mjs 相同：只注入模块依赖，目录适配逻辑原样执行。
function setup(t, { path = '/api-testing/interfaces', folders = [], items = [], totals = {} } = {}) {
  const source = assetListSource.match(/<script setup>([\s\S]*?)<\/script>/)[1].replace(/^import .*\n/gm, '')
  const calls = [], navigation = [], scope = effectScope()
  const listGet = async (requestPath, params) => {
    calls.push({ method: 'get', path: requestPath, params: utilities.copy(params ?? {}) })
    const skip = params?.skip || 0
    const limit = params?.limit || items.length
    return { data: { total: totals[requestPath] ?? items.length, items: utilities.copy(items.slice(skip, skip + limit)) } }
  }
  const api = {
    getEnvironments: async () => ({ data: [] }),
    apiTesting: {
      async get(requestPath, params) {
        if (requestPath === '/folders') {
          calls.push({ method: 'get', path: requestPath, params: utilities.copy(params ?? {}) })
          return { data: utilities.copy(folders) }
        }
        return listGet(requestPath, params)
      },
      async post(requestPath, payload) { calls.push({ method: 'post', path: requestPath, payload: utilities.copy(payload) }); return { data: {} } },
      async put(requestPath, payload) { calls.push({ method: 'put', path: requestPath, payload: utilities.copy(payload) }); return { data: {} } },
      async delete(requestPath) { calls.push({ method: 'delete', path: requestPath }); return { data: {} } },
      async patch(requestPath, payload) { calls.push({ method: 'patch', path: requestPath, payload: utilities.copy(payload) }); return { data: {} } },
    },
  }
  const dependencies = {
    ...utilities, ref, computed, watch, onMounted: () => {}, onBeforeUnmount: () => {},
    useRoute: () => ({ path }), useRouter: () => ({ push: async target => navigation.push(target) }),
    useUserStore: () => ({ isAdmin: true, userInfo: { id: 1 } }),
    ElMessage: Object.fromEntries(['error', 'warning', 'success'].map(kind => [kind, () => {}])),
    ElMessageBox: { confirm: async () => {}, prompt: async () => ({ value: 'x' }) },
    api,
  }
  const factory = new Function(...Object.keys(dependencies), source +
    '\nreturn { fetchAssetFolderTree, createAssetFolder, renameAssetFolder, deleteAssetFolder, moveAssetItem, handleFolderSelect, handleOpenItem, folderId, page, rows, keyword };')
  const state = scope.run(() => factory(...Object.values(dependencies)))
  t.after(() => scope.stop())
  return { state, calls, navigation }
}

test('接口目录树同时展示目录与条目，根节点列出全部条目', async t => {
  const context = setup(t, {
    folders: [
      { id: 1, name: '订单', kind: 'interface', parent_id: null },
      { id: 2, name: '子目录', kind: 'interface', parent_id: 1 },
      { id: 9, name: '场景目录', kind: 'scenario', parent_id: null },
    ],
    items: [
      { id: 11, name: '下单', folder_id: 1 },
      { id: 12, name: '查询', folder_id: 2 },
      { id: 13, name: '未分组接口', folder_id: null },
    ],
  })
  const { tree, items } = await context.state.fetchAssetFolderTree()
  assert.equal(tree.length, 1)
  assert.deepEqual(
    { id: tree[0].id, folder_id: tree[0].folder_id, name: tree[0].name, type: tree[0].type },
    { id: 'folder-1', folder_id: 1, name: '订单', type: 'folder' }
  )
  assert.deepEqual(tree[0].children.map(node => node.id), ['folder-2', 'interface-11'])
  assert.deepEqual(tree[0].children[0].children.map(node => node.name), ['查询'])
  // 根节点「所有接口」= 全部条目（含未分组），修复「没有展示所有的用例」
  assert.deepEqual(items.map(node => [node.id, node.item_id, node.type, node.name]), [
    ['interface-11', 11, 'interface', '下单'],
    ['interface-12', 12, 'interface', '查询'],
    ['interface-13', 13, 'interface', '未分组接口'],
  ])
})

test('条目超过一页时分页拉全量，目录树不漏条目', async t => {
  const items = Array.from({ length: 1500 }, (_, index) => ({ id: index + 1, name: `接口${index + 1}`, folder_id: index % 2 ? 1 : null }))
  const context = setup(t, { folders: [{ id: 1, name: '目录', kind: 'interface', parent_id: null }], items, totals: { '/interfaces': 1500 } })
  const { items: leaves } = await context.state.fetchAssetFolderTree()
  assert.equal(leaves.length, 1500)
  const skips = context.calls.filter(call => call.path === '/interfaces').map(call => call.params.skip)
  assert.deepEqual(skips, [0, 1000])
})

test('场景目录用 scenario 形状，打开条目进入场景编辑页', async t => {
  const context = setup(t, {
    path: '/api-testing/scenarios',
    folders: [{ id: 5, name: '巡检', kind: 'scenario', parent_id: null }],
    items: [{ id: 77, name: '每日巡检', folder_id: 5 }],
  })
  const { tree, items } = await context.state.fetchAssetFolderTree()
  assert.equal(items[0].type, 'scenario')
  assert.equal(items[0].id, 'scenario-77')
  assert.equal(tree[0].type, 'folder')
  context.state.handleOpenItem(items[0])
  assert.deepEqual(context.navigation, ['/api-testing/scenarios/77/edit'])
})

test('目录新建/重命名/删除按 api-testing 接口形状适配', async t => {
  const context = setup(t, {
    folders: [{ id: 2, name: '子目录', kind: 'interface', parent_id: 1 }, { id: 1, name: '订单', kind: 'interface', parent_id: null }],
    items: [],
  })
  await context.state.createAssetFolder({ name: '新目录', parent_id: 1 })
  await context.state.fetchAssetFolderTree()
  await context.state.renameAssetFolder(2, { name: '改名' })
  await context.state.renameAssetFolder(1, { name: '根目录改名' })
  await context.state.deleteAssetFolder(2)
  const writes = context.calls.filter(call => call.method !== 'get')
  assert.deepEqual(writes, [
    { method: 'post', path: '/folders', payload: { name: '新目录', kind: 'interface', parent_id: 1 } },
    { method: 'put', path: '/folders/2', payload: { name: '改名', kind: 'interface', parent_id: 1 } },
    { method: 'put', path: '/folders/1', payload: { name: '根目录改名', kind: 'interface', parent_id: null } },
    { method: 'delete', path: '/folders/2' },
  ])
})

test('选中目录过滤列表并翻回第一页', async t => {
  const context = setup(t, { items: [] })
  context.state.page.value = 3
  context.state.keyword.value = '下单'
  context.state.handleFolderSelect(9)
  await flush()
  assert.equal(context.state.folderId.value, 9)
  assert.equal(context.state.page.value, 1)
  const list = context.calls.find(call => call.path === '/interfaces')
  assert.equal(list.params.folder_id, 9)
})

test('api-testing 目录复刻 UI 自动化：使用共享 FolderTreePanel 且接入条目拖拽移动', () => {
  assert.match(assetListSource, /<FolderTreePanel/)
  assert.match(assetListSource, /:title="isInterface \? '接口目录' : '场景目录'"/)
  assert.match(assetListSource, /:all-label="isInterface \? '所有接口' : '所有场景'"/)
  assert.match(assetListSource, /item-id-key="item_id"/)
  // 拖拽进文件夹：move-item 接移动接口，item-moved 后刷新列表（与 UI 自动化一致）
  assert.match(assetListSource, /:move-item="moveAssetItem"/)
  assert.match(assetListSource, /@item-moved="load"/)
  // 自绘目录树已删除，样式由共享面板统一
  assert.doesNotMatch(assetListSource, /<el-tree/)
  assert.doesNotMatch(assetListSource, /folderTree\s*=\s*computed/)
})

test('moveAssetItem 调用 PATCH /{kind}/{id}/move 且带 folder_id', async t => {
  const context = setup(t, { items: [{ id: 12, name: '查询', folder_id: 1 }] })
  await context.state.moveAssetItem(12, 9)
  const move = context.calls.find(call => call.method === 'patch')
  assert.equal(move.path, '/interfaces/12/move')
  assert.deepEqual(move.payload, { folder_id: 9 })

  await context.state.moveAssetItem(12, null)
  const root = context.calls.filter(call => call.method === 'patch')[1]
  assert.deepEqual(root.payload, { folder_id: null }, 'folder_id 为空应允许移出到未分组根节点')
})

test('FolderTreePanel 外壳拖拽调宽，无移动接口时隐藏拖拽移动', () => {
  assert.match(folderTreePanel, /useResizableAside/)
  assert.match(folderTreePanel, /class="aside-resizer"/)
  assert.match(folderTreePanel, /:style="\{ width: panelWidth \+ 'px' \}"/)
  assert.match(folderTreePanel, /moveItem: \{ type: Function, default: null \}/)
  assert.match(folderTreePanel, /:draggable="canMoveItems"/)
})

test('用例与场景目录同样使用面板外壳宽度（不再写死 176px）', () => {
  for (const source of [caseList, scenarioList]) {
    assert.doesNotMatch(source, /width="176px"/)
    assert.doesNotMatch(source, /<el-aside/)
    assert.match(source, /class="folder-aside"/)
    assert.match(source, /<FolderTreePanel/)
    assert.match(source, /:move-item=/)
  }
})
