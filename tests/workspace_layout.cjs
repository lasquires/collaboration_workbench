const assert = require('node:assert/strict');
const { radialLayout, ancestryPath } = require('../docs/workspace-layout.js');

const graphOf = (parents) => ({ nodes: Object.entries(parents).map(([id, parent_id]) => ({ id, parent_id })) });

function distance(a, b) { return Math.hypot(a.x - b.x, (a.y - b.y) * 82); }

{
  const graph = graphOf({ root: null, a: 'root', b: 'root', a1: 'a', a2: 'a', b1: 'b', b2: 'b' });
  const before = JSON.stringify(graph);
  const layout = radialLayout(graph);
  assert.equal(layout.positions.size, graph.nodes.length);
  assert.deepEqual([...layout.children.get('root')], ['a', 'b']);
  assert.equal(JSON.stringify(graph), before, 'layout does not mutate the graph');
  assert.deepEqual([...layout.positions.keys()], [...radialLayout(graph).positions.keys()]);
  for (const p of layout.positions.values()) assert.ok(Number.isFinite(p.x) && Number.isFinite(p.y) && Number.isFinite(p.angle));
  for (const ring of layout.rings) {
    const ringPoints = [...layout.positions.values()].filter(p => p.island === ring.island && p.depth === ring.depth);
    for (const p of ringPoints) assert.ok(Math.abs(Math.hypot(p.x - ring.x, p.y * 82 - ring.y) - ring.radius) < 1e-8);
  }
  assert.ok(layout.width > 0 && layout.height > 0);
}

{
  const graph = graphOf({ a: null, b: 'a', c: 'b', d: 'c' });
  const layout = radialLayout(graph, { root: 'b', depth: 1 });
  assert.deepEqual([...layout.positions.keys()], ['b', 'c']);
  assert.equal(layout.positions.get('b').depth, 0);
  assert.equal(layout.positions.get('c').depth, 1);
  assert.deepEqual([...radialLayout(graph, { depth: 1 }).positions.keys()], ['a', 'b']);
  assert.deepEqual(ancestryPath(graph, 'd'), ['a', 'b', 'c', 'd']);
  assert.deepEqual(ancestryPath(graph, 'missing'), []);
}

{
  const graph = graphOf({ a: null, b: 'outside', c: 'a', d: null, e: 'd' });
  const layout = radialLayout(graph);
  assert.equal(layout.positions.size, 5);
  assert.equal(layout.positions.get('b').island, 'b');
  assert.notEqual(layout.positions.get('a').island, layout.positions.get('d').island);
  assert.ok(distance(layout.positions.get('a'), layout.positions.get('d')) > 100);
}

{
  const parents = { root: null };
  for (let i = 0; i < 30; i++) parents[`child${String(i).padStart(2, '0')}`] = 'root';
  const layout = radialLayout(graphOf(parents));
  const points = [...layout.positions.entries()].filter(([id]) => id !== 'root').map(([, p]) => p);
  for (let i = 0; i < points.length; i++) for (let j = i + 1; j < points.length; j++) {
    assert.ok(distance(points[i], points[j]) >= 13.9, 'crowded same-depth marks retain about 14 logical pixels');
  }
}

{
  const graph = graphOf({ a: 'b', b: 'a', c: null });
  const layout = radialLayout(graph);
  assert.equal(layout.positions.size, 3);
  assert.deepEqual(ancestryPath(graph, 'a'), ['b', 'a']);
}

console.log('workspace radial layout checks passed');
