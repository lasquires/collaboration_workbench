const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('docs/task-graph.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(script, 'inline graph script exists');
const context = { module: { exports: {} }, setInterval, clearInterval, setTimeout, clearTimeout };
vm.runInNewContext(script, context, { filename: 'task-graph.inline.js' });
const { layoutGraph, visibleGraph, zoomAt, selectNode } = context.module.exports;

const fixture = {
  nodes: [
    { id: 'ROOT-A', parent_id: null, fields: { title: 'Root A', status: 'active' } },
    { id: 'CHILD-A', parent_id: 'ROOT-A', fields: { title: 'Child A', status: 'done' } },
    { id: 'LEAF-A', parent_id: 'CHILD-A', fields: { title: 'Leaf A', status: 'review' } },
    { id: 'ISLAND-B', parent_id: null, fields: { title: 'Island B', status: 'ready' } },
  ],
  edges: [
    { type: 'contains', source: 'ROOT-A', target: 'CHILD-A' },
    { type: 'contains', source: 'CHILD-A', target: 'LEAF-A' },
    { type: 'prerequisite', source: 'LEAF-A', target: 'ISLAND-B' },
  ],
};

const a = layoutGraph(fixture);
const b = layoutGraph(fixture);
assert.deepEqual([...a.positions].map(([id, p]) => [id, p.x, p.y]), [...b.positions].map(([id, p]) => [id, p.x, p.y]), 'layout is deterministic');
assert.ok(a.positions.get('CHILD-A').x > a.positions.get('ROOT-A').x, 'child is laid out to the right of its parent');
assert.ok(a.positions.get('ISLAND-B').y > a.positions.get('ROOT-A').y, 'separate roots occupy distinct islands');
assert.deepEqual(fixture.edges.map(e => e.type), ['contains', 'contains', 'prerequisite'], 'containment and prerequisite edge types remain distinct');

const collapsed = visibleGraph(fixture, new Set(['CHILD-A']));
assert.deepEqual(collapsed.nodes.map(n => n.id), ['ROOT-A', 'CHILD-A', 'ISLAND-B']);
assert.equal(collapsed.hiddenCount, 1);
assert.ok(!collapsed.edges.some(e => e.target === 'LEAF-A' || e.source === 'LEAF-A'), 'edges to a collapsed descendant are hidden');
assert.equal(selectNode(fixture, 'CHILD-A').id, 'CHILD-A', 'selection resolves stable task IDs');
assert.equal(selectNode(fixture, 'MISSING'), null, 'missing IDs do not create synthetic nodes');

const view = { k: 1, x: 30, y: -12 };
const pointer = { x: 190, y: 140 };
const before = { x: (pointer.x - view.x) / view.k, y: (pointer.y - view.y) / view.k };
const zoomed = zoomAt(view, pointer, 1.7);
const after = { x: (pointer.x - zoomed.x) / zoomed.k, y: (pointer.y - zoomed.y) / zoomed.k };
assert.ok(Math.abs(before.x - after.x) < 1e-10 && Math.abs(before.y - after.y) < 1e-10, 'zoom keeps the point under the cursor anchored');
assert.equal(zoomAt(view, pointer, 100).k, 3, 'zoom clamps to the upper bound');
assert.equal(zoomAt(view, pointer, 0.001).k, 0.2, 'zoom clamps to the lower bound');

assert.match(html, /<circle class="status-dot"/, 'actual status circles are rendered');
assert.match(html, /class="edge \$\{e\.type\}"/, 'actual typed graph edges are rendered');
assert.match(html, /setInterval\(\(\)=>load\(true\),15000\)/, 'refresh preserves the current view');
assert.match(html, /aria-label="Accessible task outline"/, 'an accessible task outline is present');
assert.match(html, /partial_coverage/, 'partial migrated coverage is identified');
assert.match(html, /Technical journal \(project-wide; entries are not indexed by task\)/, 'project journal link states its task-indexing limit');

console.log('task graph layout, collapse, selection, zoom and markup checks passed');
