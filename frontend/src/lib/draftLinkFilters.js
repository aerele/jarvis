import { allRowFields, draftParentDoc } from "./draftApply";

export class DraftLinkFilterError extends Error {}
// A Dynamic Link whose controlling field (its DocType) is not set yet: there is nothing to
// search until it is, and the message names that field.
export class DraftLinkTargetError extends DraftLinkFilterError {}

const fieldName = /^[a-zA-Z_][a-zA-Z0-9_]*$/;
const forbidden = new Set(["__proto__", "prototype", "constructor"]);
const unsupported = () =>
	new DraftLinkFilterError(
		"This field's filters need ERPNext. Open the document there to choose a value."
	);

function resolveValue(value, doc, parent) {
	if (Array.isArray(value)) return value.map((v) => resolveValue(v, doc, parent));
	if (typeof value === "string" && value.startsWith("eval:")) {
		// Only field references, never executable JavaScript. Desk's arbitrary
		// expressions require its form runtime; do not drop an unresolved filter.
		const match = /^eval:\s*(doc|parent)\.([a-zA-Z_][a-zA-Z0-9_]*)\s*$/.exec(value);
		if (!match || forbidden.has(match[2])) throw unsupported();
		const context = match[1] === "parent" ? parent : doc;
		const result = context && Object.hasOwn(context, match[2]) ? context[match[2]] : undefined;
		if (result === undefined || result === null || result === "") {
			const label = match[2].replaceAll("_", " ");
			throw new DraftLinkFilterError(
				`Set ${
					label.charAt(0).toUpperCase() + label.slice(1)
				} before searching this field.`
			);
		}
		return result;
	}
	if (value !== null && typeof value === "object") throw unsupported();
	return value;
}

// `target` is the DocType being searched: a Link's options, or a Dynamic Link's current
// DocType. A Dynamic Link may carry filters for several DocTypes (four-part entries); only the
// target's apply, the others are skipped. A three-part entry applies to the target.
export function resolveLinkFilters(field, doc, parent = null, target = field.options) {
	const dynamic = field.fieldtype === "Dynamic Link";
	const filters = new Map();
	for (let source of [field.link_query_filters, field.link_filters]) {
		if (!source) continue;
		if (typeof source === "string") {
			try {
				source = JSON.parse(source);
			} catch {
				throw unsupported();
			}
		}
		if (!Array.isArray(source)) throw unsupported();
		for (const filter of source) {
			if (!Array.isArray(filter) || ![3, 4].includes(filter.length)) throw unsupported();
			const [doctype, name, operator, value] =
				filter.length === 3 ? [target, ...filter] : filter;
			if (dynamic && filter.length === 4 && doctype !== target) continue;
			if (
				doctype !== target ||
				!fieldName.test(name) ||
				forbidden.has(name) ||
				typeof operator !== "string"
			)
				throw unsupported();
			// DocField filters override a scripted filter on the same field, as in
			// Frappe's ControlLink.set_custom_query. Resolve only the final values.
			filters.set(name, [doctype, name, operator, value]);
		}
	}
	return [...filters.values()].map(([doctype, name, operator, value]) => [
		doctype,
		name,
		operator,
		resolveValue(value, doc, parent),
	]);
}

export function draftLinkSearch(model, key) {
	if (!model || !key) throw unsupported();
	const doc = draftParentDoc(model);
	let field, row;
	if (key.startsWith("f:")) {
		field = model.fields.find((f) => f.fieldname === key.slice(2));
	} else {
		// "t:{table index}:{row __key}:{fieldname}[:form]". The row is found by its
		// __key, not its position: a row removed above it moves it up a place.
		const [, ti, rk, name] = key.split(":");
		const table = model.tables[Number(ti)];
		// A grid cell or a field of the row form: both search with its meta.
		field = table && allRowFields(table).find((c) => c.fieldname === name);
		row = table?.rows.find((r) => r.__key != null && String(r.__key) === rk);
		if (!row) throw unsupported();
		row = {
			...row,
			name: row.__name || "",
			doctype: table.child,
			parent: model.docName || "",
			parenttype: model.doctype,
			parentfield: table.fieldname,
		};
	}
	const dynamic = !!field && field.fieldtype === "Dynamic Link";
	if (!field || !(field.fieldtype === "Link" || dynamic) || !field.options) throw unsupported();
	const context = row || doc;
	// A Dynamic Link searches the DocType its controlling field holds (Desk's behaviour).
	const target = dynamic ? String((context && context[field.options]) || "") : field.options;
	if (!target) {
		const fields = key.startsWith("f:")
			? model.fields
			: allRowFields(model.tables[Number(key.split(":")[1])]);
		const control = (fields || []).find((f) => f.fieldname === field.options);
		const label = (control && control.label) || field.options.replaceAll("_", " ");
		throw new DraftLinkTargetError(`Set ${label} before searching this field.`);
	}
	return {
		doctype: target,
		referenceDoctype: model.doctype,
		linkFieldname: field.fieldname,
		filters: resolveLinkFilters(field, context, row ? doc : null, target),
	};
}

export function draftLinkContext(model, key) {
	try {
		return JSON.stringify(draftLinkSearch(model, key));
	} catch {
		return null;
	}
}
