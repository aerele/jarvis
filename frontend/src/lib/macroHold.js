// An admin's hold on a macro (jarvis.chat.macros_admin_api.admin_hold): what its
// owner is told. The same sentence the server refuses a Run or a switch-on with
// (jarvis_macro.held_message), so the banner and a refusal never disagree.

// The macro's hold from a get_macro / list payload: null when it is not held, or
// from a server that does not send the fields yet (an older server holds nothing).
export function macroHold(data) {
	if (!data || !Number(data.admin_hold)) return null;
	return { reason: String(data.admin_hold_reason || "").trim() };
}

// "An admin has put this macro on hold: <reason>. It will not run until an admin
// releases it." The reason's own full stop is not doubled.
export function holdMessage(hold) {
	if (!hold) return "";
	const reason = String(hold.reason || "")
		.trim()
		.replace(/\.+$/, "");
	const said = reason
		? `An admin has put this macro on hold: ${reason}.`
		: "An admin has put this macro on hold.";
	return `${said} It will not run until an admin releases it.`;
}
