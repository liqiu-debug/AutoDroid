import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const creationSource = await readFile(
  new URL('../src/views/special/Compatibility.vue', import.meta.url),
  'utf8',
)
const reportSource = await readFile(
  new URL('../src/views/reports/CompatibilityReportDetail.vue', import.meta.url),
  'utf8',
)

test('compatibility deep links fetch the requested inspection report and gate the default selection', () => {
  assert.match(creationSource, /api\.getInspectionRun\(numericRunId\)/)
  assert.match(creationSource, /const hasExplicitRouteSource = await loadRouteInspectionRun\(\)/)
  assert.match(creationSource, /if \(!hasExplicitRouteSource && !replayForm\.inspection_run_id/)
  assert.match(creationSource, /v-if="routeSourceError"/)
})

test('compatibility report keeps ids in technical details and separates boundary evidence', () => {
  const template = reportSource.slice(reportSource.indexOf('<template>'))
  const technicalIndex = template.indexOf('<el-collapse class="technical-collapse">')
  assert.ok(technicalIndex > 0)
  const ordinaryTemplate = template.slice(0, technicalIndex)
  assert.doesNotMatch(ordinaryTemplate, /selectedResult\.(?:path_key|chain_id|source_state_id|source_observation_id)/)
  assert.match(template, /sourceBoundaryEvidenceLabel\(selectedResult\.source_boundary_evidence\)/)
  assert.match(template, /replayBoundaryEvidenceLabel\(selectedResult\.replay_boundary_evidence\)/)
  assert.match(template, /<span>State ID<\/span>/)
  assert.match(template, /<span>Observation ID<\/span>/)
})

test('compatibility report deduplicates the unknown source-version warning', () => {
  assert.match(reportSource, /sourceVersionUnknown/)
  assert.match(reportSource, /isUnknownSourceWarning/)
  assert.match(reportSource, /!\(sourceVersionUnknown\.value && isUnknownSourceWarning\(item\)\)/)
})
