/**
 * Parse a backend "list field" that rides as either a JSON-array string
 * (most listing fields - tools_required, min_apps, doctypes_required - are
 * stored as JSON text) or an already-parsed array, into a plain string array.
 *
 * Split out of AgentDetail.vue (jarvis#1062 polish): `needs` (tools_required +
 * min_apps) and `readsRecords` (doctypes_required) each reimplemented this
 * exact parse-or-passthrough byte for byte. One helper, one place to fix a
 * parse bug.
 *
 * @param {unknown} value - a JSON-array string, an array, or nullish/anything
 *   else.
 * @returns {string[]} the parsed values, coerced to strings; [] for anything
 *   that isn't (or doesn't parse to) an array.
 */
export function parseListField(value) {
	let v = value;
	if (typeof v === "string" && v.trim()) {
		try {
			v = JSON.parse(v);
		} catch (e) {
			v = null;
		}
	}
	return Array.isArray(v) ? v.map(String) : [];
}
