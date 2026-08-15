// TF-IDF content similarity + content-based suggested connections (pure).
// No embeddings, no LLM: tokenize title+summary, build normalized TF-IDF vectors,
// cosine via an INVERTED INDEX for top-k (R4 — not O(n²) all-pairs). Empty-content
// docs are skipped (R8). Content suggestions BOOTSTRAP the graph when structural
// methods are cold (R7 — an edgeless wiki still gets useful "you should link"
// hints, which is how a sparse LLM-wiki starts to densify).

const STOP = new Set(
	(
		"the a an of on in to for and or but with without from by as at is are was were be been " +
		"this that these those it its i we you they he she our your their my his her no not into " +
		"over under about above below up down out off then than so if else per via etc has have " +
		"had will shall may can could should would do does did what when where which who whom why"
	).split(" ")
);

function tokenize(text) {
	const out = [];
	for (const t of String(text || "")
		.toLowerCase()
		.split(/[^a-z0-9]+/)) {
		if (t.length > 2 && !STOP.has(t)) out.push(t);
	}
	return out;
}

function _vectorize(pages) {
	const docs = [],
		df = new Map();
	for (const p of pages) {
		const toks = tokenize([p.label, p.summary].filter(Boolean).join(" "));
		if (!toks.length) continue; // skip empty content (R8)
		const tf = new Map();
		for (const t of toks) tf.set(t, (tf.get(t) || 0) + 1);
		for (const t of tf.keys()) df.set(t, (df.get(t) || 0) + 1);
		docs.push({ id: p.id, slug: p.slug, label: p.label || p.slug, tf });
	}
	const N = docs.length;
	const vecs = docs.map((d) => {
		let norm = 0;
		const raw = new Map();
		for (const [t, c] of d.tf) {
			const idf = Math.log((N + 1) / ((df.get(t) || 0) + 1)) + 1;
			const w = (1 + Math.log(c)) * idf;
			raw.set(t, w);
			norm += w * w;
		}
		norm = Math.sqrt(norm) || 1;
		const v = new Map();
		for (const [t, w] of raw) v.set(t, w / norm);
		return v;
	});
	const inv = new Map();
	vecs.forEach((v, i) => {
		for (const [t, w] of v) {
			if (!inv.has(t)) inv.set(t, []);
			inv.get(t).push([i, w]);
		}
	});
	return { docs, vecs, inv, N };
}

// slug -> [{id, slug, label, score}] — the top-k most content-similar pages.
export function computeSimilarity(nodes, opts = {}) {
	const topK = opts.topK || 5;
	const minSim = opts.minSim == null ? 0.12 : opts.minSim;
	const pages = (nodes || []).filter((n) => n.kind === "page");
	const { docs, vecs, inv, N } = _vectorize(pages);
	const similar = {};
	for (let i = 0; i < N; i++) {
		const dot = new Map();
		for (const [t, w] of vecs[i]) {
			const postings = inv.get(t) || [];
			if (postings.length > 200) continue; // ubiquitous term — skip (perf, R4)
			for (const [j, wj] of postings) {
				if (j !== i) dot.set(j, (dot.get(j) || 0) + w * wj);
			}
		}
		const ranked = [...dot.entries()]
			.filter(([, s]) => s >= minSim)
			.sort((a, b) => b[1] - a[1])
			.slice(0, topK)
			.map(([j, s]) => ({
				id: docs[j].id,
				slug: docs[j].slug,
				label: docs[j].label,
				score: Math.round(s * 100) / 100,
			}));
		if (ranked.length) similar[docs[i].slug] = ranked;
	}
	return { similar, docCount: N };
}

// Content-similar page PAIRS that are NOT already linked → "you should connect".
export function suggestionsFromSimilar(similar, nodes, edges, opts = {}) {
	const linked = new Set();
	for (const e of edges || []) {
		if (e.kind !== "links-to") continue;
		linked.add(e.source < e.target ? `${e.source}|${e.target}` : `${e.target}|${e.source}`);
	}
	const label = {},
		idBySlug = {};
	for (const n of nodes || []) {
		if (n.kind !== "page") continue;
		label[n.id] = n.label || n.slug;
		idBySlug[n.slug] = n.id;
	}
	const seen = new Set(),
		out = [];
	for (const [slug, sims] of Object.entries(similar)) {
		const a = idBySlug[slug];
		for (const s of sims) {
			const b = s.id;
			if (!a || !b || a === b) continue;
			const key = a < b ? `${a}|${b}` : `${b}|${a}`;
			if (linked.has(key) || seen.has(key)) continue;
			seen.add(key);
			out.push({
				a,
				b,
				aLabel: label[a],
				bLabel: s.label,
				score: s.score,
				source: "content",
			});
		}
	}
	return out.sort((x, y) => y.score - x.score).slice(0, opts.max || 12);
}

export function contentSuggestions(nodes, edges, opts = {}) {
	const { similar } = computeSimilarity(nodes, opts);
	return suggestionsFromSimilar(similar, nodes, edges, opts);
}
