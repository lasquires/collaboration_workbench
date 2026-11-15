(function (root) {
  'use strict';

  function nodesOf(graph) { return Array.isArray(graph && graph.nodes) ? graph.nodes : []; }
  function ancestryPath(graph, id) {
    const nodes = nodesOf(graph);
    const byId = new Map();
    for (const node of nodes) if (node && node.id != null && !byId.has(node.id)) byId.set(node.id, node);
    if (!byId.has(id)) return [];
    const reverse = [], seen = new Set();
    let current = id;
    while (byId.has(current) && !seen.has(current)) {
      seen.add(current);
      reverse.push(current);
      const parent = byId.get(current).parent_id;
      if (parent == null || !byId.has(parent) || seen.has(parent)) break;
      current = parent;
    }
    return reverse.reverse();
  }

  function radialLayout(graph, options = {}) {
    const nodes = nodesOf(graph);
    const byId = new Map();
    for (const node of nodes) if (node && node.id != null && !byId.has(node.id)) byId.set(node.id, node);
    const children = new Map([...byId.keys()].map(id => [id, []]));
    for (const node of byId.values()) {
      if (node.parent_id != null && children.has(node.parent_id)) children.get(node.parent_id).push(node.id);
    }

    const positions = new Map(), rings = [];
    const limit = options.depth == null ? Infinity : Math.max(0, Number(options.depth) || 0);
    const chosen = options.root != null && byId.has(options.root) ? [options.root] : null;
    const roots = chosen || [...byId.values()].filter(node => node.parent_id == null || !byId.has(node.parent_id)).map(node => node.id);
    const assigned = new Set();
    const islandData = [];

    // Each loaded root is laid out independently; any remaining cyclic component
    // gets a deterministic fallback root so malformed parent loops stay finite.
    const pendingRoots = [...roots];
    while (pendingRoots.length || (!chosen && assigned.size < byId.size)) {
      if (!pendingRoots.length) {
        const fallback = [...byId.keys()].find(id => !assigned.has(id));
        if (fallback === undefined) break;
        pendingRoots.push(fallback);
      }
      const rootId = pendingRoots.shift();
      if (assigned.has(rootId)) continue;
      const local = new Set();
      const treeChildren = new Map();
      const depths = new Map([[rootId, 0]]);
      const stack = [rootId];
      while (stack.length) {
        const id = stack.pop();
        if (local.has(id) || depths.get(id) > limit) continue;
        local.add(id); assigned.add(id);
        const included = [];
        if (depths.get(id) < limit) {
          for (const child of children.get(id) || []) {
            if (local.has(child) || depths.has(child)) continue;
            depths.set(child, depths.get(id) + 1);
            included.push(child);
          }
        }
        treeChildren.set(id, included);
        for (let i = included.length - 1; i >= 0; i--) stack.push(included[i]);
      }
      if (!chosen) {
        const cover = [rootId], covered = new Set();
        while (cover.length) {
          const id = cover.pop();
          if (covered.has(id)) continue;
          covered.add(id); assigned.add(id);
          for (const child of children.get(id) || []) cover.push(child);
        }
      }
      if (!local.size) continue;

      // Postorder leaf-slot assignment centers each parent's angle on its visible descendants.
      const order = [], todo = [[rootId, false]];
      while (todo.length) {
        const [id, done] = todo.pop();
        if (done) { order.push(id); continue; }
        todo.push([id, true]);
        const kids = treeChildren.get(id) || [];
        for (let i = kids.length - 1; i >= 0; i--) todo.push([kids[i], false]);
      }
      const slot = new Map();
      let leafCount = 0;
      for (const id of order) {
        const kids = treeChildren.get(id) || [];
        if (!kids.length) slot.set(id, [leafCount++, leafCount]);
        else slot.set(id, [kids[0] === undefined ? 0 : slot.get(kids[0])[0], slot.get(kids[kids.length - 1])[1]]);
      }
      const span = Math.max(1, leafCount);
      const angles = new Map();
      for (const id of local) {
        const interval = slot.get(id);
        angles.set(id, ((interval[0] + interval[1]) / 2) * Math.PI * 2 / span - Math.PI / 2);
      }

      const atDepth = new Map();
      for (const id of local) {
        const depth = depths.get(id);
        if (!atDepth.has(depth)) atDepth.set(depth, []);
        atDepth.get(depth).push(id);
      }
      const radii = new Map([[0, 0]]);
      let previous = 0, extent = 0;
      for (const depth of [...atDepth.keys()].sort((a, b) => a - b)) {
        if (depth === 0) continue;
        const ids = atDepth.get(depth).sort((a, b) => angles.get(a) - angles.get(b));
        let needed = 0;
        for (let i = 0; i < ids.length; i++) {
          const gap = ids.length < 2 ? Math.PI * 2 : ((angles.get(ids[(i + 1) % ids.length]) - angles.get(ids[i]) + Math.PI * 2) % (Math.PI * 2));
          if (gap > 1e-10) needed = Math.max(needed, 14 / (2 * Math.sin(Math.min(Math.PI, gap) / 2)));
        }
        const radius = Math.max(previous + 70, needed);
        radii.set(depth, radius); previous = radius; extent = Math.max(extent, radius);
      }
      islandData.push({ rootId, local, depths, angles, radii, atDepth, extent });
    }

    const cell = Math.max(260, ...islandData.map(item => item.extent * 2 + 100));
    const columns = Math.max(1, Math.ceil(Math.sqrt(islandData.length)));
    for (let i = 0; i < islandData.length; i++) {
      const item = islandData[i], cx = (i % columns) * cell + cell / 2, cy = Math.floor(i / columns) * cell + cell / 2;
      for (const [depth, ids] of item.atDepth) {
        const radius = item.radii.get(depth) || 0;
        for (const id of ids) {
          const angle = item.angles.get(id);
          positions.set(id, { x: cx + radius * Math.cos(angle), y: (cy + radius * Math.sin(angle)) / 82,
            island: item.rootId, depth, angle });
        }
        rings.push({ x: cx, y: cy, radius, depth, island: item.rootId });
      }
    }
    const width = Math.max(500, columns * cell), height = Math.max(300, Math.ceil(islandData.length / columns) * cell);
    return { positions, children, width, height, rings };
  }

  const api = { radialLayout, ancestryPath };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.WorkspaceLayouts = api;
})(typeof window !== 'undefined' ? window : globalThis);
