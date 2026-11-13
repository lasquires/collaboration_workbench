const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const graphHtml = fs.readFileSync('docs/task-graph.html', 'utf8');
const dashboardHtml = fs.readFileSync('docs/roadmap-dashboard.html', 'utf8');
const source = graphHtml.match(/<script>([\s\S]*?)<\/script>/)[1];
const dashboardSource = dashboardHtml.match(/<script>([\s\S]*?)<\/script>/)[1];
assert.doesNotThrow(() => new vm.Script(source), 'graph inline script parses');
assert.doesNotThrow(() => new vm.Script(dashboardSource), 'dashboard inline script parses');
const context = { module: { exports: {} }, globalThis: {}, URLSearchParams, encodeURIComponent };
vm.runInNewContext(source, context);
const model = context.module.exports;
const plain = value => JSON.parse(JSON.stringify(value));

// Root paging must survive graph rendering, which owns #unplaced.
const rootPaging = {innerHTML:''}, neighborhoodFooter = {innerHTML:'neighborhood controls'};
const rootSelect = {value:'root',innerHTML:''};
const rootRenderScope = {rootItems:[{id:'root',parent_id:null,title:'Root'}],
  taskCursor:'page-2',focusRoot:'root',esc:value=>value,
  $:selector=>({'#rootPaging':rootPaging,'#unplaced':neighborhoodFooter,
    '#rootSelect':rootSelect,'#moreRoots':null})[selector]};
vm.runInNewContext(source.slice(source.indexOf('function renderRoots()'),
  source.indexOf('async function expandNeighborhood'))+';renderRoots();', rootRenderScope);
assert.match(rootPaging.innerHTML,/moreRoots/, 'root pagination owns a persistent toolbar container');
assert.equal(neighborhoodFooter.innerHTML,'neighborhood controls',
  'root paging never replaces neighborhood controls');

assert.equal(model.mergePreviewUrl('p /','keep?','old&'),
  '/v1/tasks/merge-preview?project_id=p+%2F&survivor_id=keep%3F&absorbed_id=old%26');
const mergeSections = model.mergePreviewSections({survivor_id:'a',absorbed_id:'b',
  field_conflicts:{title:{survivor:{present:true,value:'<script>bad</script>'},absorbed:{present:false}}},
  metadata_conflicts:{},proposed_children:[],proposed_links:[],source_union:['source'],
  claim_conflicts:[],problems:[{kind:'dependency_cycle'}]});
assert.ok(mergeSections.some(section=>section.lines.some(line=>line.includes('<script>bad</script>'))),
  'preview formatter keeps untrusted text as data for textContent rendering');
assert.ok(mergeSections.some(section=>section.lines.some(line=>line.includes('absent'))),
  'missing fields stay distinct from empty or null values');
assert.ok(mergeSections.some(section=>section.lines.some(line=>line.includes('dependency cycle'))));
assert.match(graphHtml, /id="mergePreviewForm"/);
assert.match(graphHtml, /line\.textContent=text/);

assert.deepEqual(plain(model.outlineRoots({nodes:[
  {id:'focus',parent_id:'outside-page'}, {id:'child',parent_id:'focus'},
  {id:'island',parent_id:null}
]})).map(node=>node.id), ['focus','island'],
  'accessible outline includes the visible root of a focused descendant branch');

// Slice 2 model contract: navigation changes only the view, and API progress
// indicators are derived from source fields rather than task status guesses.
const workspaceFixture = JSON.parse(fs.readFileSync('tests/fixtures/research_workspace_v1.json', 'utf8'));
assert.equal(typeof model.focusTask, 'function', 'focus navigation model is exported');
assert.equal(typeof model.backFocus, 'function', 'back navigation model is exported');
assert.equal(typeof model.applyNeighborhood, 'function', 'bounded neighborhood projection is exported');
assert.equal(typeof model.nodeIndicators, 'function', 'progress indicators are exported');
assert.equal(typeof model.nodeProgress, 'function', 'render and preview resolve progress without an undefined helper');
assert.deepEqual(plain(model.nodeProgress(workspaceFixture.nodes[0])), workspaceFixture.nodes[0].progress);
assert.equal(model.initialRoot([{id:'child',parent_id:'parent'},{id:'parent',parent_id:null}]).id,'parent',
  'initial navigation chooses a root even when its child sorts first');
assert.equal(model.initialRoot([{id:'child',parent_id:'unloaded-parent'}]),null,
  'a page containing only descendants does not invent an island');
assert.equal(typeof model.v1TransactionPayload, 'function', 'scoped v1 transaction payload is exported');
assert.equal(typeof model.neighborhoodUrl, 'function', 'bounded neighborhood URL builder is exported');
const navStart = { root: 'root', selected: 'child', view: { k: 1.7, x: -34, y: 81 }, back: [], drafts: { status: 'blocked' }, expanded: new Set(['root']), depth: 2 };
const focused = model.focusTask(navStart, 'child');
assert.equal(focused.root, 'child');
assert.deepEqual(plain(focused.view), plain(navStart.view), 'Focus keeps the current viewport in the back stack');
assert.equal(focused.back.length, 1);
const backed = model.backFocus(focused);
assert.equal(backed.root, 'root', 'Back restores the previous focused root');
assert.equal(backed.selected, 'child', 'Back restores the previous selection');
assert.deepEqual(plain(backed.view), plain(navStart.view), 'Back restores the previous pan and zoom');
assert.deepEqual([...backed.expanded], ['root'], 'Back restores the prior expanded branches');
assert.deepEqual(plain(backed.drafts), plain(navStart.drafts), 'Back preserves unsaved drafts');
assert.equal(navStart.root, 'root', 'focus does not mutate its input state');
const createPayload = model.v1TransactionPayload(workspaceFixture.project_id, 'principal-local', workspaceFixture.revision, 'request-1', [model.createOp('ALIAS-1', 'New', '')]);
assert.equal(createPayload.actor.principal_id, 'principal-local');
assert.equal(createPayload.operations[0].alias, 'ALIAS-1');
assert.equal('id' in createPayload.operations[0].record, false, 'v1 creates send aliases separately from immutable IDs');
assert.equal(createPayload.operations[0].record.title, 'New');
assert.match(model.neighborhoodUrl(workspaceFixture.project_id, workspaceFixture.root_id, 2, 250, 'cursor-a'), /cursor=cursor-a/);
assert.match(model.contextUrl(workspaceFixture.project_id, workspaceFixture.root_id), /\/context\?project_id=/);
const neighborhood = model.applyNeighborhood({ nodes: [], edges: [], expanded: new Set(), drafts: navStart.drafts }, workspaceFixture);
assert.equal(neighborhood.root, workspaceFixture.root_id);
assert.equal(neighborhood.nodes.length, workspaceFixture.nodes.length, 'fixture map nodes match API neighborhood nodes');
assert.equal(neighborhood.edges[0].source, workspaceFixture.edges[0].source_id);
assert.equal(neighborhood.frontier[0].has_more_children, true, 'unloaded child branches remain visible');
assert.deepEqual(plain(neighborhood.drafts), plain(navStart.drafts), 'bounded updates preserve unsaved drafts');
const nextPage = model.applyNeighborhood(neighborhood, { ...workspaceFixture, root_id: workspaceFixture.root_id, nodes: [{ ...workspaceFixture.nodes[0], title: 'updated root' }, { ...workspaceFixture.nodes[1], id: 'page-two', parent_id: workspaceFixture.root_id }], edges: [{ id: 'page-edge', kind: 'contains', source_id: workspaceFixture.root_id, target_id: 'page-two' }], frontier: [{ id: 'page-two', has_more_children: false }] });
assert.equal(nextPage.nodes.length, 3, 'paged neighborhoods merge bounded results without dropping prior pages');
assert.equal(nextPage.edges.length, 2);
assert.equal(nextPage.nodes.find(n => n.id === workspaceFixture.root_id).title, 'updated root', 'later page refreshes an existing node');
const parentIndicators = model.nodeIndicators(workspaceFixture.nodes[0].progress, workspaceFixture.nodes[0].status);
assert.equal(parentIndicators.fill, 'yellow', 'accepted child work is yellow while the own gate is pending');
assert.equal(parentIndicators.claimRing, 'live', 'live claim ring remains independent of yellow progress');
assert.equal(parentIndicators.required.accepted, workspaceFixture.nodes[0].progress.required.accepted_done);
assert.equal(parentIndicators.required.total, workspaceFixture.nodes[0].progress.required.total);
assert.equal(model.nodeIndicators({ own_acceptance: 'passed', required: { total: 2, accepted_done: 2 } }, 'done').fill, 'green', 'green requires both passed own acceptance and done status');
assert.equal(model.nodeIndicators({ own_acceptance: 'pending', required: { total: 0, accepted_done: 0 }, optional: { total: 1, by_state: { done: 1 } } }, 'done').fill, 'neutral', 'optional-only completion does not fabricate a required-child percentage');
const expired = model.nodeIndicators({ own_acceptance: 'pending', claim: { state: 'expired' }, unresolved_failure: true, failed_descendant_tasks: 1 }, 'blocked');
assert.equal(expired.claimHandoff, true, 'expired claims signal handoff');
assert.equal(expired.failure, true, 'own failure is separate from descendant failure count');
assert.equal(expired.failedDescendants, 1, 'failed descendants are displayed as distinct-task count');
const leafIndicators = model.nodeIndicators(workspaceFixture.nodes[1].progress, workspaceFixture.nodes[1].status);
assert.equal(leafIndicators.fill, 'neutral', 'done status alone cannot imply verified completion');
assert.equal(leafIndicators.acceptance, 'unknown');
assert.equal(leafIndicators.review, true);
assert.equal(leafIndicators.required, null, 'leaf does not display a manufactured 0/0 percentage');

const flatTasks = [
  { id: 'A', parent_id: null, sibling_order: 0, record: { title: 'Island A', status: 'active' } },
  { id: 'A.1', parent_id: 'A', sibling_order: 0, record: { title: 'Child' } },
  { id: 'A.1.a', parent_id: 'A.1', sibling_order: 0, record: { title: 'Deep child' } },
  { id: 'B', parent_id: null, sibling_order: 1, record: { title: 'Island B' } },
];
const snapshot = {
  schema_version: 1, store_id: 'fixture', revision: 7, partial_coverage: true,
  tasks: flatTasks,
  links: [{ source_id: 'A', target_id: 'B', kind: 'related', rationale: 'fixture', review_state: 'unreviewed' }],
  events: [{ task_id: 'A', kind: 'observation', details: { text: 'fixture journal' } }],
  nodes: flatTasks.map(t => ({ id: t.id, parent_id: t.parent_id, fields: t.record, provenance: { source_path: `fixture/${t.id}` } })),
  edges: [{ type: 'contains', source: 'A', target: 'A.1' }, { type: 'contains', source: 'A.1', target: 'A.1.a' }, { type: 'related', source: 'A', target: 'B' }],
  coverage: { node_count: 4, island_count: 2 },
};
assert.equal(model.snapshotTasks(snapshot).length, 4, 'flat task snapshot retained');
const projected = model.projectSnapshot(snapshot);
assert.equal(projected.nodes.length, 4);
assert.equal(projected.nodes.find(n => n.id === 'A.1.a').fields.title, 'Deep child');
assert.equal(projected.edges.length, 3);
assert.equal(model.selectNode(projected, 'A.1.a').parent_id, 'A.1');
assert.deepEqual(plain(model.createOp('C', 'New island', 'summary')), { op: 'create', record: { id: 'C', title: 'New island', status: 'ready', summary: 'summary' } });
assert.deepEqual(plain(model.createOp('C.1', 'Nested', '', 'C')), { op: 'create', record: { id: 'C.1', title: 'Nested', status: 'ready', summary: '' }, parent_id: 'C' });
assert.deepEqual(plain(model.linkOp('A', 'B', 'related', 'rationale')), { op: 'add_link', task_id: 'A', target_id: 'B', kind: 'related', rationale: 'rationale', review_state: 'unreviewed' });
assert.deepEqual(plain(model.taskOp('journal', 'A', { kind: 'observation', text: 'note' })), { op: 'journal', task_id: 'A', kind: 'observation', text: 'note' });
const conflictedDraft = { status: 'blocked', evidence: 'draft-report.md' };
const latestValues = { status: 'done', evidence: 'latest-report.md' };
const conflicts = model.makeConflicts(conflictedDraft, latestValues);
assert.deepEqual(plain(conflicts.map(c => [c.key, c.draft, c.latest])), [['status', 'blocked', 'done'], ['evidence', 'draft-report.md', 'latest-report.md']]);
assert.equal(model.canMutate(conflicts), false, 'a revision conflict blocks mutations');
assert.equal(model.resolveConflicts(conflicts, { status: 'draft' }), null, 'every conflicting field must be resolved');
assert.equal(conflictedDraft.status, 'blocked', 'conflict inspection does not mutate the retained draft');
const acknowledged = model.resolveConflicts(conflicts, { status: 'draft', evidence: 'latest' });
assert.deepEqual(plain(acknowledged), { status: 'blocked', evidence: 'latest-report.md' });
assert.equal(model.canMutate(null), true, 'explicit resolution unlocks mutation');
const latestNeighborhood = model.projectSnapshot(workspaceFixture);
assert.equal(latestNeighborhood.nodes[0].id, workspaceFixture.root_id);
assert.equal(model.makeConflicts(conflictedDraft, { status: latestNeighborhood.nodes[0].status || 'active', evidence: '' })[0].latest, 'active', 'conflict comparison accepts a normalized full snapshot');
const layout = model.layoutGraph(projected);
assert.equal(layout.positions.size, 4, 'deep finite tree receives positions');
assert.equal(layout.positions.get('B').island, 'B', 'second island kept distinct');
assert.ok(layout.positions.get('A.1.a').x > layout.positions.get('A.1').x);
for (const [width, height] of [[320, 500], [1500, 800]]) {
  const many = { nodes: Array.from({ length: 90 }, (_, i) => ({ id: `N${i}`, parent_id: i ? `N${Math.floor((i - 1) / 3)}` : null })) };
  const bounds = model.layoutGraph(many), fit = model.fitView(bounds, width, height);
  assert.ok(fit.k >= 0.01 && fit.k <= 1.5, 'fit scale stays within safe limits');
  for (const p of bounds.positions.values()) {
    assert.ok(fit.x + fit.k * (p.x - 72) >= -1e-8, `left node bound fits ${width}px viewport`);
    assert.ok(fit.x + fit.k * (p.x + 72) <= width + 1e-8, `right node bound fits ${width}px viewport`);
    assert.ok(fit.y + fit.k * (p.y * 82 - 25) >= -1e-8, `top node bound fits ${height}px viewport`);
    assert.ok(fit.y + fit.k * (p.y * 82 + 25) <= height + 1e-8, `bottom node bound fits ${height}px viewport`);
  }
  const xs = [...bounds.positions.values()].map(p => p.x);
  const centerX = (Math.min(...xs) + Math.max(...xs)) / 2;
  assert.ok(Math.abs(fit.x + fit.k * centerX - width / 2) < 1e-8, 'fit centers the rendered node bounds');
}
const view = { selected: 'A.1.a', view: { k: 1.7, x: -34, y: 81 }, collapsed: new Set(['A']), drafts: { status: 'blocked' } };
const refreshed = model.preserveRefresh(projected, projected, view);
assert.equal(refreshed.selected, 'A.1.a');
assert.deepEqual({ ...refreshed.view }, view.view);
assert.deepEqual({ ...refreshed.drafts }, view.drafts, 'refresh state keeps drafts');
assert.ok(refreshed.collapsed.has('A'));
const removed = model.preserveRefresh(projected, { nodes: [{ id: 'B' }] }, view);
assert.equal(removed.selected, 'B', 'selection falls back only if removed');
assert.equal(removed.collapsed.size, 0);

for (const html of [graphHtml, dashboardHtml]) {
  assert.match(html, /\/(?:api\/transaction|v1\/transactions)/, 'mutations use SQLite transaction API');
  assert.match(html, /expected_revision/);
  assert.match(html, /request_id/);
  assert.match(html, /Revision conflict|revision conflict|Conflict at revision/i, 'conflict is explicit');
  assert.match(html, /draft/i, 'draft retention communicated');
  assert.match(html, /Partial task coverage|Partial task coverage|partial_coverage/);
  assert.match(html, /forensic/i, 'workflow and forensic states separated');
  assert.doesNotMatch(html, /\/api\/task-update|\/api\/claim|board\.board|data\.board/);
}
for (const fragment of ['Create island or nested task', 'Add task link', 'Append journal', 'Evidence paths', 'Claim task', 'Release claim'])
  assert.ok(graphHtml.includes(fragment) || dashboardHtml.includes(fragment), `form/control present: ${fragment}`);
assert.match(graphHtml, /role="button"/);
assert.match(graphHtml, /onkeydown/);
assert.match(graphHtml, /prefers-reduced-motion/);
assert.match(graphHtml, /@media\(max-width:520px\)/);
assert.match(dashboardHtml, /@media\(max-width:650px\)/);
assert.match(graphHtml, /viewport changes only the viewport|Dragging never changes task relationships/);
for (const operation of ['op:"create"', 'op:"update"', 'op:"claim"', 'op:"release"', 'op:"journal"', 'op:"add_link"'])
  assert.ok(graphHtml.includes(operation) || dashboardHtml.includes(operation), `transaction payload operation: ${operation}`);
assert.match(graphHtml, /if\(parent_id\)operation\.parent_id=parent_id/, 'nested create carries parent ID');
assert.match(graphHtml, /function linkOp\(task_id,target_id,kind,rationale\)/, 'link form carries source, target, type and rationale');
assert.match(graphHtml, /showConflict\(drafts,latest\)/, 'revision conflict retains the full latest graph for field comparison');
for (const endpoint of ['/v1/projects', '/v1/tasks?', '/neighborhood?', '/context?project_id=']) assert.ok(graphHtml.includes(endpoint), `map uses bounded v1 endpoint ${endpoint}`);
assert.match(graphHtml, /id="projectSelect"/);
assert.match(graphHtml, /id="rootSelect"/);
assert.match(graphHtml, /async function expandNeighborhood/);
assert.match(graphHtml, /v1TransactionPayload\(projectId,localPrincipalId,graph\.revision/);
assert.match(graphHtml, /await expandNeighborhood\(id,false\);fitCurrent\(\)/, 'focus loads a bounded neighborhood and restores view without a POST');
assert.match(graphHtml, /ctx\.fields/);
assert.match(graphHtml, /ctx\.reservations/);
assert.match(graphHtml, /Load next page/);
console.log('task graph UI fixture checks passed');
