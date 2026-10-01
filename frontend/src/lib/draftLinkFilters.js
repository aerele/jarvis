import { coerceOut } from "./draftApply";

export class DraftLinkFilterError extends Error {}

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

export function resolveLinkFilters(field, doc, parent = null) {
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
				filter.length === 3 ? [field.options, ...filter] : filter;
			if (
				doctype !== field.options ||
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
	const doc = { ...model.linkContext, doctype: model.doctype, name: model.docName || "" };
	for (const f of model.fields) doc[f.fieldname] = coerceOut(f);
	let field, row;
	if (key.startsWith("f:")) {
		field = model.fields.find((f) => f.fieldname === key.slice(2));
	} else {
		const [, ti, ri, name] = key.split(":");
		const table = model.tables[Number(ti)];
		field = table?.columns.find((c) => c.fieldname === name);
		row = table?.rows[Number(ri)];
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
	if (!field || field.fieldtype !== "Link" || !field.options) throw unsupported();
	return {
		doctype: field.options,
		referenceDoctype: model.doctype,
		linkFieldname: field.fieldname,
		filters: resolveLinkFilters(field, row || doc, row ? doc : null),
	};
}

export function draftLinkContext(model, key) {
	try {
		return JSON.stringify(draftLinkSearch(model, key));
	} catch {
		return null;
	}
}
