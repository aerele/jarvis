// Keep the card unless the Confirm answer settled it, and say why; "" = the card is
// spent. A "no" without a reason_code is a legacy card's: its token is used. Mirrors
// the SPA's lib/chatCardActions.js keepsChatCard.
const KEPT = {
	busy: "This action is being handled right now. Try again in a moment.",
	executing: "This action is already running.",
	identity_refused:
		"This action can no longer run as the user it was proposed for. Nothing ran.",
	armed_run: "This macro run is stopping; the action was withdrawn and nothing ran.",
	// approve_and_run (C5, mirrors the SPA's chatCardActions.js): the server
	// keeps the response shape + one of these on every refusal, and the card
	// stays Pending on all of them - only the words live here.
	not_runnable: "This card no longer offers Approve & run. Use Confirm instead.",
	needs_own_confirm:
		"This action must be confirmed on its own, not run automatically. Use Confirm instead.",
	skill_not_armed:
		"The skill this would run is no longer armed. Use Confirm instead, or arm it again.",
	storage_unavailable: "Couldn't reach confirmation storage. Try again in a moment.",
	storage_outcome_unknown: "Lost track of whether that went through. Check before retrying.",
};
const SETTLED_STATUS = ["Executed", "Failed", "Discarded", "Cancelled", "Superseded"];
const SETTLED_CODE = ["not_found", "already_handled"];

export function keptCardMessage(res) {
	if (!res || res.ok !== false || !res.reason_code) return "";
	if (SETTLED_STATUS.includes(res.pa_status) || SETTLED_CODE.includes(res.reason_code))
		return "";
	return KEPT[res.reason_code] || res.error?.message || "This action could not be completed.";
}

// D2/D3: cards never expire now, so an Approve on a card whose target changed
// settles it as Failed with a specific reason_code (mirrors the SPA's
// chatCardActions.js REFUSALS). keptCardMessage correctly treats it as
// settled (returns "" above), so DecisionSheet's InvalidConfirmation branch
// checks THIS instead, to say why rather than guess "handled elsewhere".
const SETTLED_REASONS = {
	stale: "The record changed after this was proposed. Nothing ran.",
	target_missing: "The record this targets no longer exists. Nothing ran.",
	tampered: "This action failed an integrity check. Nothing ran.",
	unverifiable: "This action can no longer be verified on this site. Nothing ran.",
};
export function settledReasonMessage(res) {
	if (!res || res.ok !== false || !res.reason_code) return "";
	return SETTLED_REASONS[res.reason_code] || "";
}
