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

// The one line in the sticky footer, next to the button the person just pressed. The banner that
// carries the message sits at the top of a body that scrolls, so after a failed Save it can be out
// of sight; this line says it where the thumb is. null when there is nothing to say.
//   errorLine  errorLineOf(panel.error);  dead  the card is no longer pending
//   deadText   the words of the dead banner
// A message that marks fields has its own scroll to the first mark and says "Fix the marked fields".
export function footerStatus({ errorLine, dead, deadText, marked = false }) {
	if (dead) return { text: deadText, tone: "error" };
	if (errorLine)
		return {
			text: marked ? "Fix the marked fields, then save again." : errorLine.message,
			tone: "error",
		};
	return null;
}

// Is the banner inside the area the person is looking at? `banner` and `area` are the rects of the
// banner and of the form's scrolling area (getBoundingClientRect). An element with no height (not
// laid out, jsdom) is "not known to be in view": the footer then keeps saying the message, which is
// the safe side. Used to say a message once: the footer repeats it only while the banner is out of sight.
export function bannerVisible(banner, area) {
	if (!banner || !area || !(banner.height > 0) || !(area.height > 0)) return false;
	return banner.top >= area.top - 1 && banner.bottom <= area.bottom + 1;
}
