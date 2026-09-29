// Child-table rows a write card proposes, keyed by table fieldname for apply_action.
// The rows go as proposed: the server edits a row that carries its `name` in place
// (keeping fields the card does not show), adds a row without one, and refuses a
// table with no row names on a document that already has rows, instead of wiping
// it (CR-3). A table the doctype does not have is an error, never a silent skip.
export function cardTableValues(action, meta) {
	const known = (meta && meta.tables) || {};
	const out = {};
	for (const t of action.tables || []) {
		if (!t || !t.fieldname) continue;
		if (!(t.fieldname in known))
			throw new Error(`"${t.fieldname}" is not a table on this document type`);
		if (t.rows != null && !Array.isArray(t.rows))
			throw new Error(`"${t.fieldname}" rows are not a list`);
		out[t.fieldname] = t.rows || [];
	}
	return out;
}

// One read-only line per proposed table, so the phone never applies rows it did
// not mention.
export function cardTableSummary(action) {
	const out = [];
	for (const t of action.tables || []) {
		if (!t || !t.fieldname) continue;
		const n = Array.isArray(t.rows) ? t.rows.length : 0;
		out.push({ label: t.label || t.fieldname, value: n === 1 ? "1 row" : `${n} rows` });
	}
	return out;
}

// On an update the proposed rows are the table's final set, so a saved row the
// agent did not name would be deleted. The phone card cannot show those rows, so
// it refuses instead: returns one message per table that would lose rows, or [].
// `saved` is load_doc's `tables` ({fieldname: [{name, ...}]}).
export function cardTableRemovals(action, saved) {
	const out = [];
	for (const t of action.tables || []) {
		if (!t || !t.fieldname) continue;
		const kept = new Set(
			(Array.isArray(t.rows) ? t.rows : []).map((r) => r && r.name).filter(Boolean)
		);
		const lost = ((saved || {})[t.fieldname] || []).filter((r) => !kept.has(r.name));
		if (lost.length)
			out.push(
				`${lost.length} saved ${lost.length === 1 ? "row" : "rows"} in ${
					t.label || t.fieldname
				}`
			);
	}
	return out;
}
