// Pure field-control helpers shared by ChatView's record draft panel and the
// standalone DocFieldsForm (moved out of ChatView.vue; behaviour unchanged).
import { checkToYesNo, normDateVal } from "@/lib/draftApply";

// Map a Frappe fieldtype → the edit control to render + its options payload.
export function controlFor(fieldtype, options) {
	switch (fieldtype) {
		case "Link":
			return ["link", options || ""]; // options = target doctype (searchLink)
		case "Select":
			return [
				"select",
				String(options || "")
					.split("\n")
					.map((o) => o.trim()),
			];
		case "Check":
			return ["check", ""];
		case "Date":
			return ["date", ""];
		case "Datetime":
			return ["datetime", ""];
		case "Time":
			return ["time", ""];
		case "Int":
		case "Float":
		case "Currency":
		case "Percent":
		case "Rating":
			return ["number", ""];
		case "Small Text":
		case "Text":
		case "Long Text":
		case "Code":
		case "Text Editor":
		case "HTML Editor":
		case "Markdown Editor":
		case "JSON":
			return ["text", ""];
		default:
			return ["data", ""];
	}
}

// A grid Select cell's options: the field's own, plus the current value when they
// lack it (as panelField does for a main field), so a cell never silently blanks.
export function cellOptions(column, value) {
	const options = controlFor("Select", column.options)[1];
	const v = value == null ? "" : String(value);
	return v && !options.includes(v) ? [v, ...options] : options;
}

export function panelField(metaField, value) {
	let [control, options] = controlFor(metaField.fieldtype, metaField.options);
	let v = value == null ? "" : String(value);
	if (["date", "datetime", "time"].includes(control)) v = normDateVal(metaField.fieldtype, v);
	let orig = v;
	if (control === "check") {
		v = checkToYesNo(v);
		orig = v;
	}
	if (control === "select" && Array.isArray(options) && v && !options.includes(v))
		options = [v, ...options];
	return {
		fieldname: metaField.fieldname,
		label: metaField.label,
		control,
		options,
		fieldtype: metaField.fieldtype,
		reqd: metaField.reqd,
		read_only: metaField.read_only,
		link_filters: metaField.link_filters,
		link_query_filters: metaField.link_query_filters,
		value: v,
		orig,
	};
}

// Mark the fields a failed create named (apply_action `error.fields`) on a draft
// model. A field meta does not mark required (mandatory_depends_on) is added from
// the form meta so the person can fill it. Child-row misses stay in the message.
export function markMissing(model, missing, metaFields = []) {
	for (const m of missing || []) {
		if (m.parentfield) continue;
		let field = model.fields.find((f) => f.fieldname === m.fieldname);
		if (!field) {
			const metaField = metaFields.find((f) => f.fieldname === m.fieldname);
			if (!metaField) continue;
			field = panelField(metaField, "");
			model.fields.push(field);
		}
		field.serverMissing = true;
	}
}
