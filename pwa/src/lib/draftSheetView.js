// What the phone's draft sheet writes on screen, as pure functions (node --test runs them).

// The header's title, as the desktop panel writes it: "New Timesheet", "Update Sales Order · SO-1".
export function sheetTitle(model) {
	if (!model) return "";
	const verb = model.verb === "update" ? "Update" : "New";
	return `${verb} ${model.doctype || ""}${model.docName ? ` · ${model.docName}` : ""}`.trim();
}

// The error line over the form. `error` is what the form model carries: a plain string (the
// card's own error line, copied when a plain Create from the form was refused) or an envelope
// ({message, hint, fields...}) from a refused Save. null when there is nothing to say.
export function errorLineOf(error) {
	if (!error) return null;
	if (typeof error === "string")
		return error.trim() ? { message: error.trim(), hint: "" } : null;
	const message = typeof error.message === "string" ? error.message.trim() : "";
	if (!message) return null;
	return { message, hint: typeof error.hint === "string" ? error.hint.trim() : "" };
}
