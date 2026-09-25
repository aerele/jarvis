// Keep the card unless the Confirm answer settled it, and say why; "" = the card is
// spent. A "no" without a reason_code is a legacy card's: its token is used. Mirrors
// the SPA's lib/chatCardActions.js keepsChatCard.
const KEPT = {
	busy: "This action is being handled right now. Try again in a moment.",
	executing: "This action is already running.",
	identity_refused:
		"This action can no longer run as the user it was proposed for. Nothing ran.",
	armed_run: "This macro run is stopping; the action was withdrawn and nothing ran.",
};
const SETTLED_STATUS = ["Executed", "Failed", "Discarded", "Cancelled", "Superseded"];
const SETTLED_CODE = ["not_found", "already_handled"];

export function keptCardMessage(res) {
	if (!res || res.ok !== false || !res.reason_code) return "";
	if (SETTLED_STATUS.includes(res.pa_status) || SETTLED_CODE.includes(res.reason_code))
		return "";
	return KEPT[res.reason_code] || res.error?.message || "This action could not be completed.";
}
