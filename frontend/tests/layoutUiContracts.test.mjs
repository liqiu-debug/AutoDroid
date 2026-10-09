import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const read = path => readFile(new URL(`../src/${path}`, import.meta.url), 'utf8')
const [styles, layout, taskList, changePassword, caseEditor, stepBuilder, dashboard] = await Promise.all([
  read('assets/main.css'),
  read('layout/Index.vue'),
  read('views/tasks/TaskList.vue'),
  read('views/account/ChangePassword.vue'),
  read('views/cases/CaseEditor.vue'),
  read('components/StepBuilder.vue'),
  read('views/dashboard/DashboardView.vue'),
])

test('confirm boxes keep a fixed width instead of stretching across the viewport', () => {
  // Element Plus sets width:100% and caps with max-width, so overriding only
  // max-width made every confirm box span the whole screen.
  assert.match(
    styles,
    /\.el-message-box \{ width: var\(--el-messagebox-width, 420px\); max-width: calc\(100vw - 32px\);/,
  )
  // Narrow screens stay inside the viewport.
  assert.match(styles, /body\.ad-mobile \.el-message-box \{ max-width: calc\(100vw - 16px\); \}/)
})

test('the account menu sits in the header instead of the sidebar footer', () => {
  const start = layout.indexOf('<el-header class="layout-header">')
  const header = layout.slice(start, layout.indexOf('</el-header>', start))
  assert.ok(start >= 0 && header.length > 0, 'layout header must exist')
  assert.match(header, /<Navbar \/>/)
  assert.match(header, /<ClientModeSwitch \/>/)
  assert.doesNotMatch(layout, /sidebar-account/)
  assert.doesNotMatch(layout, /<footer class="sidebar-account">/)
})

test('the execution-content column centres both its header and its content', () => {
  assert.match(taskList, /<el-table-column label="执行内容" min-width="260" align="center">/)
  const rule = taskList.slice(taskList.indexOf('.execution-cell {'))
  assert.match(rule.slice(0, rule.indexOf('}')), /justify-content: center/)
})

test('the change-password title stays next to the back button', () => {
  const rule = changePassword.slice(changePassword.indexOf('.page-header {'))
  // ad-page-header is space-between, which pushed the title to the far right.
  assert.match(rule.slice(0, rule.indexOf('}')), /justify-content: flex-start/)
})

test('the case editor keeps general steps as a column next to a bounded step list', () => {
  assert.match(caseEditor, /grid-template-columns: minmax\(360px, 1fr\) 220px 350px/)
  assert.match(caseEditor, /<section class="actions-pane">\s*<GeneralStepsPanel/)
  // The drawer was the replacement for the column; both at once is redundant.
  assert.doesNotMatch(caseEditor, /<el-drawer v-model="actionsVisible"/)
  assert.doesNotMatch(caseEditor, /添加动作/)
})

test('step cards keep the per-action colour that tells step types apart', () => {
  assert.match(stepBuilder, /:style="\{ '--action-color': getActionColor\(step\.action\) \}"/)
  const card = stepBuilder.slice(stepBuilder.indexOf('.step-card {'))
  assert.match(card.slice(0, card.indexOf('}')), /border-left: 3px solid var\(--action-color\)/)
  const index = stepBuilder.slice(stepBuilder.indexOf('.step-index {'))
  assert.match(index.slice(0, index.indexOf('}')), /background: var\(--action-color\)/)
})

test('the dashboard shows interface automation in its own block', () => {
  assert.match(dashboard, /api_automation: null/)
  assert.match(dashboard, /const apiBlock = computed\(\(\) => overview\.value\.api_automation \|\| null\)/)
  assert.match(dashboard, /<span>接口自动化<\/span>/)
  assert.match(dashboard, /router\.push\('\/execution\/reports\?tab=api'\)/)
  assert.match(dashboard, /router\.push\(`\/execution\/reports\/api\/\$\{row\.id\}`\)/)
  // API runs never carry WARNING, so the API trend omits that series.
  assert.match(dashboard, /buildTrendOption\(apiBlock\.value\?\.trend \|\| \[\], \{ includeWarning: false \}\)/)
})
