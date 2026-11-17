// Value adapters for <DateControl> (#833): the frappe-ui pickers on one side, the
// strings the old native inputs spoke on the other (what the card's save path,
// normDateVal and the dirty check all expect). Pure, so they are unit-tested.

// "YYYY-MM-DD" from whatever DatePicker emits ("" stays "").
export function toDate(v) {
	const m = String(v == null ? "" : v).match(/^(\d{4}-\d{2}-\d{2})/);
	return m ? m[1] : "";
}

// "HH:mm" from "HH:mm" or "HH:mm:ss" (TimePicker keeps seconds when it has them).
export function toTime(v) {
	const m = String(v == null ? "" : v).match(/^(\d{2}:\d{2})/);
	return m ? m[1] : "";
}

// The card's "YYYY-MM-DDTHH:mm" (or a stored "YYYY-MM-DD HH:mm:ss") as the
// "YYYY-MM-DD HH:mm:ss" DateTimePicker reads.
export function toPickerDatetime(v) {
	const m = String(v == null ? "" : v).match(
		/^(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2})(:\d{2})?)?/
	);
	if (!m) return "";
	return `${m[1]} ${m[2] || "00:00"}${m[3] || ":00"}`;
}

// DateTimePicker's "YYYY-MM-DD HH:mm:ss" back to the card's "YYYY-MM-DDTHH:mm".
export function fromPickerDatetime(v) {
	const m = String(v == null ? "" : v).match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/);
	return m ? `${m[1]}T${m[2]}` : "";
}

// The attributes DateControl moves onto the picker's <input>: the caller's id and
// aria-* (passed as fallthrough attrs) and the aria-label prop. null removes one.
export const INPUT_ATTRS = ["id", "aria-label", "aria-describedby", "aria-required"];
export function inputAttrsFor(attrs, ariaLabel) {
	const out = {};
	for (const name of INPUT_ATTRS) {
		let v = name === "aria-label" ? ariaLabel || (attrs && attrs[name]) : attrs && attrs[name];
		if (v === false || v === "" || v === undefined) v = null;
		out[name] = v == null ? null : String(v);
	}
	return out;
}

// The input's own classes: the look's input class (so the callers' state rules
// reach it) plus any extra ones, as a de-duplicated list.
export function inputClassesFor(look, extra) {
	const base = look === "ask" ? "jv-ask-field" : "jv-action-input";
	const more = String(extra || "")
		.split(/\s+/)
		.filter(Boolean);
	return [...new Set([base, ...more])];
}
