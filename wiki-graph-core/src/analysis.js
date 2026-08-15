// Combined analysis: graphology structure (analyze) + TF-IDF content similarity.
// Pure — the single source the Web Worker AND the sync fallback both call.
// Merges suggested connections CONTENT-FIRST so a sparse/edgeless wiki still gets
// useful "you should link" hints (R7), with structural (Adamic-Adar) filling in.
import { analyze } from "./graphAnalysis.js";
import { computeSimilarity, suggestionsFromSimilar } from "./similarity.js";

function _mergeSuggestions(content, structural) {
	const seen = new Set(),
		out = [];
	for (const s of [...(content || []), ...(structural || [])]) {
		if (!s || !s.a || !s.b) continue;
		const key = s.a < s.b ? `${s.a}|${s.b}` : `${s.b}|${s.a}`;
		if (seen.has(key)) continue;
		seen.add(key);
		out.push(s);
	}
	return out.slice(0, 12);
}

export function fullAnalysis(data) {
	const a = analyze(data);
	const { similar } = computeSimilarity((data && data.nodes) || []);
	a.lists.similar = similar; // slug -> top content-similar pages (DetailPanel)
	const content = suggestionsFromSimilar(similar, data.nodes, data.edges);
	a.lists.suggestedLinks = _mergeSuggestions(content, a.lists.suggestedLinks);
	return a;
}
