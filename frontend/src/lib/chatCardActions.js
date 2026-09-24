// Copy for a chat action card decided on the Approval Board (Jarvis Pending Action,
// kind chat). Confirm / Discard go through the chat's own confirm_tool /
// dismiss_tool, and every answer carries a reason_code; the words live here. All
// plain text: callers escape it for an HTML sink (frappe-ui toasts).
import { isSettled } from "./heldActions";

const REFUSALS = {
	not_found: "This action is no longer waiting.",
	already_handled: "This action was already handled.",
	busy: "This action is being handled right now. Try again in a moment.",
	executing: "This action is already running.",
	armed_run: "This macro run is stopping; the action was withdrawn and nothing ran.",
	identity_refused:
		"This action can no longer run as the user it was proposed for. Nothing ran.",
	stale: "The record changed after this was proposed. Nothing ran.",
	target_missing: "The record this targets no longer exists. Nothing ran.",
	tampered: "This action failed an integrity check. Nothing ran.",
	unverifiable: "This action can no longer be verified on this site. Nothing ran.",
};

export function chatRefusalMessage(res) {
	const code = (res && res.reason_code) || "";
	const server = res && res.error && res.error.message;
	if (code === "failed" && server) return `Couldn't complete it: ${server}`;
	if (code === "partial" && server)
		return `It may have partly run: ${server} Check before retrying.`;
	return REFUSALS[code] || (server ? String(server) : "This action could not be completed.");
}

export function chatOutcomeMessage(res, action) {
	if (res && res.reason_code === "already_handled") return REFUSALS.already_handled;
	if (action === "discard") return "Discarded. Nothing ran.";
	return res && res.queued
		? "Confirmed. Jarvis continues in the chat once it's free."
		: "Confirmed. Jarvis continues in the chat.";
}

// Keep the card unless the answer settled it (busy, already running, identity
// refused, a stopping armed run). A "no" without a reason_code is a legacy card's:
// its token is spent.
export function keepsChatCard(res) {
	return !!res && res.ok === false && !!res.reason_code && !isChatSettled(res);
}

// The card left Pending (or is gone): the lane drops it. A used legacy token's
// "no longer valid" answer carries no reason_code.
export function isChatSettled(res) {
	if (isSettled(res)) return true;
	return !!res && !res.reason_code && !!res.error && res.error.type === "InvalidConfirmation";
}

// Read-only line for a card that can no longer be acted on.
export function chatStatusLine(rec) {
	if (!rec) return "";
	if (rec.status === "Executing") return "Running now…";
	if (rec.status === "Executed") return "Confirmed.";
	if (rec.status === "Discarded") return "Discarded. Nothing ran.";
	return rec.reason || "This action was already handled.";
}
