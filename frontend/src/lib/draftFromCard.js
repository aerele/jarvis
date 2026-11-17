// A server draft card -> the action the edit form's model builder takes, with EXACT
// fieldnames. The card's `draft.values` is keyed by fieldname (the typed path's actions are
// keyed by label), so each field carries `fieldname` and the builder resolves it by that
// name first, never by fuzzy label matching.
//
// Pure, relative imports only: the phone shares this file. Never throws.
import { isSensitiveCard } from "./draftCardView.js";

const isObject = (v) => !!v && typeof v === "object" && !Array.isArray(v);

// A deep copy, so a nested value (a JSON cell) is never shared with the card. A reactive
// proxy cannot be structuredClone'd, so it goes through JSON; a value JSON cannot hold (a
// cycle) falls back to a shallow copy.
function copyOf(row) {
	try {
		return structuredClone(row);
	} catch {
		try {
			return JSON.parse(JSON.stringify(row));
		} catch {
			return { ...row };
		}
	}
}

// draft.values[table] row `i` is the row `_draft_row: i` names (a create's row, or an update's
// row the draft added), so every entry keeps its place: an entry that is not a row becomes an
// empty row, never a gap.
const rowOf = (r) => (isObject(r) ? copyOf(r) : {});

// A field's value as the form shows it: a JSON field arrives as an object, shown as JSON
// text, never "[object Object]".
const fieldValue = (v) => (isObject(v) ? JSON.stringify(v) : v);

// `{kind: "doc", verb, doctype, name, fields, tables, continue, token, source: "card"}`,
// or null for a card that cannot be read or is sensitive (a `risk` card keeps its warning).
// `name` is "" when the draft has none (a create, or an unnamed one); a prompt-named create
// also holds its name in `values`, which is left as it is.
export function draftFromCard(card, token) {
	try {
		if (!isObject(card) || isSensitiveCard(card) || !isObject(card.draft)) return null;
		const d = card.draft;
		if ((d.verb !== "create" && d.verb !== "update") || !isObject(d.values)) return null;
		const fields = [];
		const tables = [];
		for (const [fieldname, value] of Object.entries(d.values)) {
			if (Array.isArray(value)) tables.push({ fieldname, rows: value.map(rowOf) });
			else fields.push({ fieldname, label: fieldname, value: fieldValue(value) });
		}
		return {
			kind: "doc",
			verb: d.verb,
			doctype: typeof d.doctype === "string" ? d.doctype : "",
			name: typeof d.name === "string" ? d.name : "",
			fields,
			tables,
			continue: d.continue ? 1 : 0,
			token: token == null ? "" : token,
			source: "card",
		};
	} catch {
		return null;
	}
}
