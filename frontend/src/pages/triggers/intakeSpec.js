// Pure helpers for the Intake trigger's `intake_spec` JSON:
// {"fields": [...], "table": {"fieldname", "columns": [...]} | null, "match": [...], "defaults": {field: value}}

export const emptySpec = () => ({ fields: [], table: null, match: [], defaults: {} });

const isBlank = (v) => v == null || (typeof v === "string" && !v.trim());

// The stored JSON string (or an object) -> a spec object with every key present.
export function parseSpec(raw) {
	let data = raw;
	if (typeof raw === "string") {
		try {
			data = raw.trim() ? JSON.parse(raw) : null;
		} catch (e) {
			data = null;
		}
	}
	if (!data || typeof data !== "object") return emptySpec();
	const table = data.table && data.table.fieldname ? data.table : null;
	return {
		fields: Array.isArray(data.fields) ? [...data.fields] : [],
		table: table ? { fieldname: table.fieldname, columns: [...(table.columns || [])] } : null,
		match: Array.isArray(data.match) ? [...data.match] : [],
		defaults: { ...(data.defaults || {}) },
	};
}

// What is saved. The server refuses an empty default (None) and a default for a
// field the file already fills, so those keys are dropped here, before saving.
export function serializeSpec(spec) {
	const s = parseSpec(spec);
	const defaults = {};
	for (const [key, value] of Object.entries(s.defaults)) {
		if (!isBlank(value) && !s.fields.includes(key)) defaults[key] = value;
	}
	return JSON.stringify({
		fields: s.fields,
		table: s.table,
		match: s.match.filter((f) => s.fields.includes(f)),
		defaults,
	});
}

// Labels of the mandatory things the spec leaves without a field, column or default.
// Same rule as the server's strict check (jarvis.intake.spec._validate_coverage).
export function uncoveredLabels(spec, options) {
	if (!options) return [];
	const s = parseSpec(spec);
	const out = [];
	for (const f of options.fields || []) {
		if (f.mandatory && !s.fields.includes(f.fieldname) && isBlank(s.defaults[f.fieldname]))
			out.push(f.label);
	}
	for (const t of options.tables || []) {
		if (s.table && s.table.fieldname === t.fieldname) {
			for (const c of t.columns || []) {
				if (c.mandatory && !s.table.columns.includes(c.fieldname))
					out.push(`${t.label}: ${c.label}`);
			}
		} else if (t.mandatory) {
			out.push(t.label);
		}
	}
	return out;
}
