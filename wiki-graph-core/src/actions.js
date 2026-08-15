// Prioritized action items from the graph (pure, both surfaces). Uses structure
// + flags available everywhere; telemetry-only items (demand/gaps) are merged in
// by the caller when present (admin). Guards degenerate/empty input (R8).
export function computeActions(data, analysis) {
	const nodes = (data && data.nodes) || [];
	const edges = (data && data.edges) || [];
	const pages = nodes.filter((n) => n.kind === "page");
	const metrics = (analysis && analysis.metrics) || {};

	const authors = {};
	for (const e of edges) {
		if (e.kind === "authored")
			(authors[e.target] = authors[e.target] || new Set()).add(e.source);
	}

	const stale = pages
		.filter((p) => p.stale || p.contradiction)
		.map((p) => ({
			slug: p.slug,
			label: p.label,
			id: p.id,
			reason: p.contradiction ? "contradiction" : "stale",
		}));

	const orphans = pages
		.filter((p) => (metrics[p.id] || {}).orphan)
		.map((p) => ({ slug: p.slug, label: p.label, id: p.id }));

	const busFactor = pages
		.filter((p) => (authors[p.id] || new Set()).size === 1)
		.map((p) => ({
			slug: p.slug,
			label: p.label,
			id: p.id,
			author: [...authors[p.id]][0].replace(/^user:/, ""),
		}));

	const norm = (t) =>
		String(t || "")
			.toLowerCase()
			.replace(/[^a-z0-9]+/g, " ")
			.trim();
	const byNorm = {};
	for (const p of pages) {
		const k = norm(p.label);
		if (k) (byNorm[k] = byNorm[k] || []).push(p);
	}
	const duplicates = Object.values(byNorm)
		.filter((g) => g.length > 1)
		.map((g) => ({ title: g[0].label, slugs: g.map((x) => x.slug) }));

	const suggest = (analysis && analysis.lists && analysis.lists.suggestedLinks) || [];
	return { stale, orphans, busFactor, duplicates, suggest };
}
