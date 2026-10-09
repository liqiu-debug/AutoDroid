import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const read = path => readFile(new URL(`../src/${path}`, import.meta.url), 'utf8')
const [scenario, interfaceEditor, assetList, assertions, resultPanel, requestEditor, importDialog, aiAssertions, aiList, aiBatch, debugComposable] = await Promise.all([
  read('views/api-testing/ScenarioEditor.vue'),
  read('views/api-testing/InterfaceEditor.vue'),
  read('views/api-testing/AssetList.vue'),
  read('components/api-testing/AssertionsEditor.vue'),
  read('components/api-testing/ResultPanel.vue'),
  read('components/api-testing/RequestEditor.vue'),
  read('components/api-testing/ImportSpecDialog.vue'),
  read('components/api-testing/AiAssertions.vue'),
  read('components/api-testing/AiSuggestionList.vue'),
  read('components/api-testing/AiBatchAssertions.vue'),
  read('composables/useApiDebug.js'),
])

test('a scenario step card shows only the method and the check count', () => {
  assert.match(scenario, /<p>\{\{ summaries\[step\.id\] \}\}<\/p>/)
  assert.match(scenario, /const summaries=computed\(\(\)=>Object\.fromEntries\(steps\.value\.map\(step=>\[step\.id,stepSummary\(step\)\]\)\)\)/)
  // The card previously deep-copied the same step twice per render.
  assert.doesNotMatch(scenario, /effectiveStep\(step\)\.request\.method/)
})

test('the scenario description stays visible and leads to its editor', () => {
  assert.match(scenario, /class="scenario-note"/)
  assert.match(scenario, /form\.description \|\| '添加说明/)
  assert.match(scenario, /@click="infoOpen=true"/)
  assert.match(assetList, /label="说明" min-width="140" show-overflow-tooltip/)
})

test('a scenario whose interfaces moved on is flagged in the list', () => {
  assert.match(assetList, /v-if="!isInterface && row\.stale_steps"/)
  assert.match(assetList, /接口有更新/)
})

test('the interface editor surfaces its usages and can sync the clean ones', () => {
  assert.match(interfaceEditor, /被 \{\{ usages\.total_scenarios \}\} 个场景的 \{\{ usages\.total_steps \}\} 个步骤引用/)
  assert.match(interfaceEditor, /\/interfaces\/\$\{id\.value\}\/usages/)
  assert.match(interfaceEditor, /syncCleanSteps/)
  assert.match(interfaceEditor, /:disabled="dirty"/)
  assert.match(interfaceEditor, /需逐项确认/)
  // Conflicts must be decided in the scenario, never applied silently.
  assert.match(interfaceEditor, /step\.stale && !step\.conflicts\.length/)
})

test('pinning a value that changes every run is warned about inline', () => {
  assert.match(assertions, /dynamicWarning/)
  assert.match(assertions, /该字段每次运行通常不同/)
  assert.match(assertions, /changeOp\(i,'not_empty'\)/)
  assert.match(assertions, /class="dynamic-warning"/)
  assert.match(assertions, /grid-column:1\/-1/)
})

test('retry is offered only where it is safe and its result is labelled', () => {
  assert.match(requestEditor, /<template v-if="\$slots\.retry">/)
  assert.match(scenario, /仅对连接失败重试；已收到响应不会重试，避免重复写入/)
  assert.match(resultPanel, /共尝试 \{\{ result\.detail\.attempts \}\} 次/)
  assert.match(resultPanel, /retry_history/)
})

test('document import parses first and creates only the selected entries', () => {
  assert.match(importDialog, /\/imports\/spec/)
  assert.match(importDialog, /\/imports\/apply/)
  assert.match(importDialog, /reserve-selection/)
  assert.match(importDialog, /勾选后才会创建接口/)
  assert.match(assetList, /ImportSpecDialog/)
  assert.match(scenario, /specOpen=true/)
})

test('AI suggestion prose restores the line height that el-checkbox-group zeroes', () => {
  // Element Plus sets font-size:0 and line-height:0 on the group; without an
  // override the explanation paragraphs collapse onto each other.
  const rule = aiList.slice(aiList.indexOf('.suggestions{'))
  assert.match(rule.slice(0, rule.indexOf('}')), /font-size:13px;line-height:1\.6/)
  assert.match(aiList, /<el-checkbox-group :model-value="modelValue" class="suggestions"/)
  // Both the single-step dialog and the batch drawer render the same rows.
  assert.match(aiAssertions, /<AiSuggestionList v-model="chosen" :suggestions="suggestions" :disabled="stale"/)
  assert.match(aiBatch, /<AiSuggestionList v-if="generated\[item\.stepId\]\.suggestions\.length"/)
  assert.doesNotMatch(aiAssertions, /<el-checkbox-group/)
})

test('batch AI assertions run once, generate for every step, then apply and recheck in order', () => {
  // Entry lives next to the other scenario-level actions and only when AI is on.
  assert.match(scenario, /<el-dropdown-item v-if="aiAvailable" command="batch" divided/)
  assert.match(scenario, /AI 校验建议（全部步骤）/)
  assert.match(scenario, /if\(!await precheck\(last\.id\)\|\|!alive\)return/)
  assert.match(scenario, /:inert="loading\|\|runStarting\|\|debugStarting\|\|batchBusy"/)
  // The drawer is appended to body so it stays usable while the editor is inert.
  assert.match(aiBatch, /append-to-body/)
  assert.match(aiBatch, /props\.debug\.run\(lastStepId, 'through', \{ confirmed: true \}\)/)
  // Every generation call happens before anything is applied, and the recheck
  // loop runs after the draft was updated.
  const generate = aiBatch.indexOf("'/ai/suggest-assertions'"), apply = aiBatch.indexOf("emit('apply', payload)")
  const recheck = aiBatch.indexOf('props.debug.recheck(item.stepId')
  assert.ok(generate >= 0 && apply > generate, 'generation must finish before apply')
  assert.ok(recheck > apply, 'recheck must follow the applied draft')
  assert.match(aiBatch, /props\.debug\.recheck\(item\.stepId, \{ silent: true \}\)/)
  // Defaults to all suggestions selected; a failed step is never a template.
  assert.match(aiBatch, /\(data\.suggestions \|\| \[\]\)\.map\(\(_, index\) => index\)/)
  assert.match(aiBatch, /batchSuggestionPlan\(props\.steps, props\.debug\.results\.value\)/)
  assert.match(aiBatch, /feedback\(result\.callId, additions\.length \? 'accepted' : 'dismissed', additions\.length\)/)
  // Applying is idempotent in both places: existing checks are skipped and shown as applied.
  assert.match(aiBatch, /newAssertions\(step \? effectiveStep\(step\)\.assertions : \[\]/)
  assert.match(aiAssertions, /newAssertions\(assertions\.value,/)
  assert.match(aiList, /:applied="appliedIndexes"|applied: \{ type: Array/)
})

test('the debug composable keeps its defaults while allowing silent batch control', () => {
  assert.match(debugComposable, /const run = async \(stepId, mode = 'single', \{ confirmed = false \} = \{\}\)/)
  assert.match(debugComposable, /if \(!confirmed\) await ElMessageBox\.confirm/)
  assert.match(debugComposable, /const recheck = async \(stepId, \{ silent = false \} = \{\}\)/)
  assert.match(debugComposable, /return \{ status: data\.result\.status, error: null \}/)
})
