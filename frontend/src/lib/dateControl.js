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
