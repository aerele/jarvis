// Pure display transforms that fight the hairball (Bloom / Linkurious / SemSpect).
// overlayFilter = which tiers to show; egoGraph = one node's neighborhood (work
// view); searchGraph = matches + their neighbors; collapseClusters = a topic map
// of community meta-nodes you expand on click. All pure → node-testable.

const NODE_KINDS = {
	knowledge: new Set(["page", "org"]),
	utilization: new Set(["page", "org", "role", "user"]),
	demand: new Set(["page", "org", "role", "user"]),
};
const EDGE_KINDS = {
	knowledge: new Set(["links-to", "scope"]),
	utilization: new Set(["links-to", "scope", "authored", "member-of"]),
	demand: new Set(["links-to", "scope", "authored", "member-of", "read", "wrote"]),
};

export function overlayFilter(data, overlay) {
	const nodes = data.nodes || [],
		edges = data.edges || [];
	if (overlay === "demand") return { nodes, edges, gaps: data.gaps };
	const nk = NODE_KINDS[overlay] || NODE_KINDS.knowledge;
	const ek = EDGE_KINDS[overlay] || EDGE_KINDS.knowledge;
	const fn = nodes.filter((n) => nk.has(n.kind));
	const ids = new Set(fn.map((n) => n.id));
	const fe = edges.filter((e) => ek.has(e.kind) && ids.has(e.source) && ids.has(e.target));
	return { nodes: fn, edges: fe, gaps: data.gaps };
}

function _adjacency(edges) {
	const adj = {};
	for (const e of edges) {
		(adj[e.source] = adj[e.source] || []).push(e.target);
		(adj[e.target] = adj[e.target] || []).push(e.source);
	}
	return adj;
}

function _ball(seeds, edges, hops) {
	const adj = _adjacency(edges);
	const keep = new Set(seeds);
	let frontier = [...keep];
	for (let h = 0; h < hops; h++) {
		const next = [];
		for (const id of frontier)
			for (const nb of adj[id] || []) {
				if (!keep.has(nb)) {
					keep.add(nb);
					next.push(nb);
				}
			}
		frontier = next;
	}
	return keep;
}

export function egoGraph(data, focusId, hops = 2) {
	const nodes = data.nodes || [],
		edges = data.edges || [];
	if (!focusId || !nodes.some((n) => n.id === focusId)) return data;
	const keep = _ball([focusId], edges, hops);
	return {
		nodes: nodes.filter((n) => keep.has(n.id)),
		edges: edges.filter((e) => keep.has(e.source) && keep.has(e.target)),
		gaps: data.gaps,
	};
}

export function searchGraph(data, query, hops = 1) {
	const q = (query || "").trim().toLowerCase();
	if (!q) return data;
	const nodes = data.nodes || [],
		edges = data.edges || [];
	const seeds = nodes
		.filter((n) => (n.label || n.id).toLowerCase().includes(q) || (n.slug || "").includes(q))
		.map((n) => n.id);
	if (!seeds.length) return { nodes: [], edges: [], gaps: data.gaps };
	const keep = _ball(seeds, edges, hops);
	return {
		nodes: nodes.filter((n) => keep.has(n.id)),
		edges: edges.filter((e) => keep.has(e.source) && keep.has(e.target)),
		gaps: data.gaps,
	};
}

export function collapseClusters(data, metrics, communities, expanded) {
	const nodes = data.nodes || [],
		edges = data.edges || [];
	expanded = expanded || new Set();
	const commOf = (id) => (metrics[id] ? metrics[id].community : null);
	const displayId = {},
		out = [],
		size = {},
		seen = new Set();
	for (const n of nodes) {
		if (n.kind !== "page") continue; // topic map = pages only
		const c = String(commOf(n.id));
		if (expanded.has(c)) {
			displayId[n.id] = n.id;
			out.push(n);
		} else {
			const cid = `cluster:${c}`;
			displayId[n.id] = cid;
			size[cid] = (size[cid] || 0) + 1;
			if (!seen.has(cid)) {
				seen.add(cid);
				out.push({
					id: cid,
					kind: "cluster",
					cluster: true,
					community: c,
					label: (communities[c] || {}).label || `Cluster ${c}`,
				});
			}
		}
	}
	for (const n of out) if (n.kind === "cluster") n.weight = size[n.id];
	const byKey = {},
		outEdges = [];
	for (const e of edges) {
		if (e.kind !== "links-to") continue;
		const s = displayId[e.source],
			t = displayId[e.target];
		if (!s || !t || s === t) continue;
		const key = s < t ? s + "|" + t : t + "|" + s;
		if (byKey[key]) {
			byKey[key].weight += 1;
			continue;
		}
		const edge = { source: s, target: t, kind: "cluster-link", weight: 1 };
		byKey[key] = edge;
		outEdges.push(edge);
	}
	return { nodes: out, edges: outEdges, gaps: data.gaps };
}
