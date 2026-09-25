// Pure helpers for Edit & create on a held File Box create (PR-2d): field keys,
// the patch the server takes, what is still missing, and the Desk escape hatch.
// A child cell's key is "table.idx.field" (idx 1-based), as the server's `locked`.

export function fieldKey(entry) {
	return entry && entry.parentfield
		? `${entry.parentfield}.${entry.idx}.${entry.fieldname}`
		: (entry && entry.fieldname) || "";
}

function valueAt(values, entry) {
	if (!entry.parentfield) return values[entry.fieldname];
	const rows = Array.isArray(values[entry.parentfield]) ? values[entry.parentfield] : [];
	const row = rows[(Number(entry.idx) || 0) - 1];
	return row && typeof row === "object" ? row[entry.fieldname] : undefined;
}

function isEmpty(v) {
	return v == null || String(v).trim() === "";
}

// needs_input entries of one doc whose value is still empty in `values`.
export function stillMissing(needsInput, docIndex, values) {
	return (needsInput || []).filter(
		(e) => (e.doc_index || 0) === docIndex && isEmpty(valueAt(values || {}, e))
	);
}

// Server errors of one doc as {key: message}.
export function errorsByKey(errors, docIndex) {
	const out = {};
	for (const e of errors || []) {
		if ((e.doc_index || 0) === docIndex && e.fieldname) out[fieldKey(e)] = e.message || "";
	}
	return out;
}

function same(a, b) {
	return String(a == null ? "" : a).trim() === String(b == null ? "" : b).trim();
}

// Only what the approver changed: a field map, a table as row patches by position.
export function patchOf(original, current) {
	const patch = {};
	for (const [key, value] of Object.entries(current || {})) {
		const before = (original || {})[key];
		if (Array.isArray(value)) {
			const rows = value.map((row, i) => {
				const was = (Array.isArray(before) && before[i]) || {};
				const cells = {};
				for (const [c, v] of Object.entries(row || {})) if (!same(v, was[c])) cells[c] = v;
				return cells;
			});
			if (rows.some((r) => Object.keys(r).length)) patch[key] = rows;
		} else if (!same(value, before)) {
			patch[key] = value;
		}
	}
	return patch;
}

// "/app/<slug>/new?field=value…": the non-secret scalar values, for the full form.
export function deskNewUrl(doctype, values, secret = []) {
	const slug = String(doctype || "")
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, "-")
		.replace(/^-+|-+$/g, "");
	const params = new URLSearchParams();
	for (const [key, value] of Object.entries(values || {})) {
		if (secret.includes(key) || value == null || typeof value === "object") continue;
		if (String(value).trim() === "") continue;
		params.set(key, String(value));
	}
	const query = params.toString();
	return `/app/${slug}/new${query ? "?" + query : ""}`;
}
