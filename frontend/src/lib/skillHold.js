// An admin's hold on a skill (jarvis.chat.skills_admin_api.admin_hold): what its
// owner is told. Used by the owner's Skills list and skill form.

// The skill's hold from a list or get_custom_skill payload: null when it is not
// held, or from a server that does not send the fields yet.
export function skillHold(data) {
	if (!data || !Number(data.admin_hold)) return null;
	return { reason: String(data.admin_hold_reason || "").trim() };
}

// "On hold by an administrator: <reason>", the reason as typed.
export function holdText(hold) {
	if (!hold) return "";
	return hold.reason
		? `On hold by an administrator: ${hold.reason}`
		: "On hold by an administrator";
}
