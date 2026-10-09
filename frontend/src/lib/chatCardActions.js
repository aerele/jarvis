// Copy for a chat action card decided on the Approval Board (Jarvis Pending Action,
// kind chat). Confirm / Discard go through the chat's own confirm_tool /
// dismiss_tool, and every answer carries a reason_code; the words live here. All
// plain text: callers escape it for an HTML sink (frappe-ui toasts).
import { personError } from "./actionSummary.js";
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
	// approve_and_run (C5): the server keeps the response shape + one of these on
	// every refusal, and the card stays Pending - keepsChatCard already keeps it
	// (none of these mark pa_status settled), so only the words live here.
	not_runnable: "This card no longer offers Approve & run. Use Confirm instead.",
	needs_own_confirm:
		"This action must be confirmed on its own, not run automatically. Use Confirm instead.",
	skill_not_armed:
		"The skill this would run is no longer armed, or was switched off. Use Confirm instead.",
	storage_unavailable: "Couldn't reach confirmation storage. Try again in a moment.",
	storage_outcome_unknown: "Lost track of whether that went through. Check before retrying.",
};

export function chatRefusalMessage(res) {
	const code = (res && res.reason_code) || "";
	// The person's words for a refusal written for the model (actionSummary.personError).
	const server = res && res.error && personError(res.error).message;
	if (code === "failed" && server) return `Couldn't complete it: ${server}`;
	if (code === "partial" && server)
		return `It may have partly run: ${server} Check before retrying.`;
	return REFUSALS[code] || (server ? String(server) : "This action could not be completed.");
}

// A next-step click (propose_next_action) refused for a reason that passes: another
// card is waiting, or the card store hiccuped. The step is still valid, so the button
// stays; any other refusal means the step is gone.
const TEMPORARY_NEXT_STEP_CODES = ["ConfirmationPendingError", "ConfirmationUnavailableError"];

export function nextStepRefusal(res) {
	const err = (res && res.error) || {};
	const temporary = TEMPORARY_NEXT_STEP_CODES.includes(err.code);
	const person = typeof err.person_message === "string" ? err.person_message.trim() : "";
	// Never the model-facing text: only the person's words, else a plain line.
	const fallback = temporary
		? "Couldn't open the card right now. Try again."
		: "That next step is not available.";
	return {
		temporary,
		message: person || (res && res.reason_code ? chatRefusalMessage(res) : fallback),
	};
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

// A card that settled (isChatSettled) still carries a SPECIFIC reason_code when
// the server could name one (stale/target_missing/tampered/unverifiable, …) -
// say that, not the opaque "another tab" guess reserved for a bare legacy
// token with no reason_code at all (keepsChatCard already routes the "keep it"
// case away from here).
export function chatSettledReason(res) {
	return res && res.ok === false && res.reason_code ? chatRefusalMessage(res) : "";
}

// Read-only line for a card that can no longer be acted on.
export function chatStatusLine(rec) {
	if (!rec) return "";
	if (rec.status === "Executing") return "Running now…";
	if (rec.status === "Executed") return "Confirmed.";
	if (rec.status === "Discarded") return "Discarded. Nothing ran.";
	return rec.reason || "This action was already handled.";
}

export function shouldHideNextStep(refusal) {
	return !refusal.temporary;
}

const NEXT_STEP_TOOLS = ["submit_doc", "apply_workflow_action"];
// A step is done once a receipt for it succeeded: confirmed on a card, or auto-applied
// without one. failed / discarded / unknown / partial leave it open.
const NEXT_STEP_DONE_OUTCOMES = ["confirmed", "auto_applied"];

export function receiptRecord(m) {
	try {
		const a = typeof m.tool_args === "string" ? JSON.parse(m.tool_args) : m.tool_args || {};
		const r = typeof m.tool_result === "string" ? JSON.parse(m.tool_result) : m.tool_result;
		const d = (r && r.data) || {};
		return { doctype: d.doctype || a.doctype, name: d.name || a.name };
	} catch (e) {
		return {};
	}
}

// "Doctype|name" keys of the records the thread's receipts already acted on, plus `gone`.
export function nextStepActedKeys(rows, gone = new Set()) {
	const keys = new Set(gone);
	for (const x of rows) {
		if (x.role !== "tool" || !NEXT_STEP_DONE_OUTCOMES.includes(x.action_outcome)) continue;
		if (!NEXT_STEP_TOOLS.includes(x.tool_name)) continue;
		const o = receiptRecord(x);
		keys.add(`${o.doctype}|${o.name}`);
	}
	return keys;
}
