// Pure field-control helpers shared by ChatView's record draft panel and the
// standalone DocFieldsForm (moved out of ChatView.vue; behaviour unchanged).
import { checkToYesNo } from "@/lib/draftApply";

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

// Native date/time inputs REQUIRE canonical values (yyyy-mm-dd / yyyy-mm-ddThh:mm);
// anything else — "2026-07-10 00:00:00", "10-07-2026" — renders the input EMPTY,
// which read as "the date isn't picking". Normalize whatever the agent/doc gave us.
export function normDateVal(fieldtype, v) {
	const s = String(v == null ? "" : v).trim();
	if (!s) return s;
	if (fieldtype === "Date") {
		let m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
		if (m) return `${m[1]}-${m[2]}-${m[3]}`;
		m = s.match(/^(\d{2})[-/](\d{2})[-/](\d{4})$/); // dd-mm-yyyy / dd/mm/yyyy
		if (m) return `${m[3]}-${m[2]}-${m[1]}`;
	}
	if (fieldtype === "Datetime") {
		let m = s.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/);
		if (m) return `${m[1]}T${m[2]}`;
		m = s.match(/^(\d{4}-\d{2}-\d{2})$/);
		if (m) return `${m[1]}T00:00`;
	}
	if (fieldtype === "Time") {
		const m = s.match(/^(\d{2}:\d{2})/);
		if (m) return m[1];
	}
	return s;
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
		value: v,
		orig,
	};
}
