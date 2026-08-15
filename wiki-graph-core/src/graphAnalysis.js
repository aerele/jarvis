// Obsidian-style "Graph Analysis" over the wiki graph, client-side (graphology).
// Pure: takes {nodes, edges, gaps, co_read}, returns per-node metrics + ranked
// lists. Communities = Louvain (named by their top page); hubs/authorities =
// HITS; brokers = betweenness (their loss fragments knowledge); suggested links =
// Adamic-Adar over the [[links-to]] graph; orphans = pages with no link.
import Graph from "graphology";
import louvain from "graphology-communities-louvain";
import hits from "graphology-metrics/centrality/hits";
import betweenness from "graphology-metrics/centrality/betweenness";

const _MAX_HUB_DEGREE = 60; // skip huge hubs in link prediction (pair blow-up)
const _MAX_SUGGEST = 12;

export function analyze(data) {
	const nodes = (data && data.nodes) || [];
	const edges = (data && data.edges) || [];
	const pages = nodes.filter((n) => n.kind === "page");

	// Full graph (all kinds) for communities + degree.
	const full = new Graph({ type: "mixed" });
	for (const n of nodes) if (n && n.id && !full.hasNode(n.id)) full.addNode(n.id);
	for (const e of edges) {
		if (!e || !full.hasNode(e.source) || !full.hasNode(e.target)) continue;
		if (e.source === e.target || full.hasEdge(e.source, e.target)) continue;
		try {
			full.addEdge(e.source, e.target, { weight: e.weight || 1 });
		} catch (_) {
			// graphology throws on a duplicate edge, and the centrality measures
			// throw on degenerate graphs. Both are normal for real wiki data - skip
			// the offending item rather than failing the whole analysis.
		}
	}
	let comm = {};
	try {
		if (full.size > 0) comm = louvain(full);
	} catch (_) {
		comm = {};
	}

	// Page-only directed [[links-to]] graph for HITS + betweenness + orphans.
	const pg = new Graph({ type: "directed", allowSelfLoops: false });
	for (const p of pages) if (!pg.hasNode(p.id)) pg.addNode(p.id);
	const adj = {}; // undirected adjacency for link prediction + orphan flag
	for (const e of edges) {
		if (!e || e.kind !== "links-to") continue;
		if (
			pg.hasNode(e.source) &&
			pg.hasNode(e.target) &&
			e.source !== e.target &&
			!pg.hasEdge(e.source, e.target)
		) {
			try {
				pg.addEdge(e.source, e.target);
			} catch (_) {
				// graphology throws on a duplicate edge, and the centrality measures
				// throw on degenerate graphs. Both are normal for real wiki data - skip
				// the offending item rather than failing the whole analysis.
			}
		}
		(adj[e.source] = adj[e.source] || new Set()).add(e.target);
		(adj[e.target] = adj[e.target] || new Set()).add(e.source);
	}
	let hb = { hubs: {}, authorities: {} };
	try {
		if (pg.size > 0) hb = hits(pg);
	} catch (_) {
		// graphology throws on a duplicate edge, and the centrality measures
		// throw on degenerate graphs. Both are normal for real wiki data - skip
		// the offending item rather than failing the whole analysis.
	}
	let bt = {};
	try {
		if (pg.size > 0) bt = betweenness(pg);
	} catch (_) {
		// graphology throws on a duplicate edge, and the centrality measures
		// throw on degenerate graphs. Both are normal for real wiki data - skip
		// the offending item rather than failing the whole analysis.
	}

	const metrics = {};
	for (const n of nodes) {
		metrics[n.id] = {
			degree: full.hasNode(n.id) ? full.degree(n.id) : 0,
			community: comm[n.id] != null ? comm[n.id] : 0,
			authority: hb.authorities[n.id] || 0,
			hub: hb.hubs[n.id] || 0,
			betweenness: bt[n.id] || 0,
			orphan: n.kind === "page" && !(adj[n.id] && adj[n.id].size),
		};
	}

	// Named communities: label each cluster by its highest-degree page.
	const byComm = {};
	for (const p of pages) (byComm[metrics[p.id].community] ||= []).push(p);
	const communities = {};
	for (const [cid, ps] of Object.entries(byComm)) {
		const top = ps.slice().sort((a, b) => metrics[b.id].degree - metrics[a.id].degree)[0];
		communities[cid] = { id: cid, label: top ? top.label || top.slug : cid, size: ps.length };
	}

	// Suggested links — Adamic-Adar over shared [[links-to]] neighbours.
	const scores = {};
	const deg = (id) => (adj[id] ? adj[id].size : 0);
	for (const z of Object.keys(adj)) {
		const ns = [...adj[z]];
		if (ns.length < 2 || ns.length > _MAX_HUB_DEGREE) continue;
		const w = 1 / Math.log(Math.max(2, deg(z)));
		for (let i = 0; i < ns.length; i++) {
			for (let j = i + 1; j < ns.length; j++) {
				const a = ns[i],
					b = ns[j];
				if (a === b || (adj[a] && adj[a].has(b))) continue; // already linked
				const key = a < b ? a + "|" + b : b + "|" + a;
				scores[key] = (scores[key] || 0) + w;
			}
		}
	}
	const label = {};
	for (const p of pages) label[p.id] = p.label || p.slug;
	const suggestedLinks = Object.entries(scores)
		.map(([k, s]) => {
			const [a, b] = k.split("|");
			return { a, b, aLabel: label[a], bLabel: label[b], score: Math.round(s * 100) / 100 };
		})
		.filter((x) => x.aLabel && x.bLabel)
		.sort((x, y) => y.score - x.score)
		.slice(0, _MAX_SUGGEST);

	const byDegree = (a, b) => metrics[b.id].degree - metrics[a.id].degree;
	const hubs = [...pages]
		.sort((a, b) => metrics[b.id].authority - metrics[a.id].authority || byDegree(a, b))
		.slice(0, 12);
	const brokers = [...pages]
		.filter((p) => metrics[p.id].betweenness > 0)
		.sort((a, b) => metrics[b.id].betweenness - metrics[a.id].betweenness)
		.slice(0, 8);
	const mostRead = pages
		.filter((n) => (n.demand || 0) > 0)
		.sort((a, b) => (b.demand || 0) - (a.demand || 0))
		.slice(0, 12);
	const debt = pages
		.filter((n) => n.debt)
		.sort((a, b) => (b.demand || 0) - (a.demand || 0))
		.slice(0, 12);
	const orphans = pages.filter((n) => metrics[n.id].orphan);
	const gaps = ((data && data.gaps) || [])
		.slice()
		.sort((a, b) => (b.asked || 0) - (a.asked || 0));
	const coRead = ((data && data.co_read) || [])
		.slice()
		.map((c) => ({
			...c,
			aLabel: label[`page:${c.a}`] || c.a,
			bLabel: label[`page:${c.b}`] || c.b,
		}))
		.sort((a, b) => (b.count || 0) - (a.count || 0))
		.slice(0, 12);

	return {
		metrics,
		communities,
		lists: { hubs, brokers, mostRead, debt, orphans, gaps, suggestedLinks, coRead },
	};
}
