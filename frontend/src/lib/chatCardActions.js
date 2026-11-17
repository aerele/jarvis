// Copy for a chat action card decided on the Approval Board (Jarvis Pending Action,
// kind chat). Confirm / Discard go through the chat's own confirm_tool /
// dismiss_tool, and every answer carries a reason_code; the words live here. All
// plain text: callers escape it for an HTML sink (frappe-ui toasts).
import { personError } from "./actionSummary.js";
import { thrownActionError } from "./chatErrorCopy.js";
import { isSettled } from "./heldActions.js";

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
	safety_update:
		"This confirmation was cancelled during an agent safety update. Submit the action again if it is still needed.",
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

const DRAFT_STALE = "This record changed after the draft was made. Nothing was saved.";

// A storage outage or an unknown outcome: the write did NOT run and the card is still pending.
// The envelope has `error.type` and, unlike every other refusal, no `reason_code` (the
// approve-and-run endpoint adds one, which keepsChatCard reads).
export function confirmationStorageUnavailable(response) {
	const type = response && response.error && response.error.type;
	return type === "ConfirmationUnavailableError" || type === "ConfirmationOutcomeUnknownError";
}

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
// `draft`: the card is a tool draft card, whose receipt says "changed after the draft was made".
export function chatSettledReason(res, { draft = false } = {}) {
	if (draft && res && res.ok === false && res.reason_code === "stale") return DRAFT_STALE;
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

// ---- what a card does with the server's answer ------------------------------------------------
//
// One decision for the desktop's Create / Confirm, Approve & run and Cancel / Discard of a pending
// card and for the phone's (the hosts only apply it). Pure: a reply in, what to do out.
//
// outcome = {
//   fate      "gone": the card is spent, take it off the list. "kept": it is still pending, usable
//             again, and `message` is shown with it.
//   message   the words for the person ("" when none: the receipt in the reloaded thread speaks)
//   cardError the object the desktop's card shows as its error when kept (null otherwise); the
//             phone shows `message`
//   toast     the desktop also shows `message` as an error toast
//   reload    the desktop reloads the chat after taking the card off. The phone always reloads a
//             card that is gone: nothing else brings it the receipt
//   queued    { run_id, message_id, position } when the confirmed card started a queued turn
//   safety    { message } when an agent safety update cancelled the card (the desktop shows its
//             one Done-only notice, the reloaded thread carries the receipt)
// }
const OUTCOME = {
	fate: "gone",
	message: "",
	cardError: null,
	toast: false,
	reload: false,
	queued: null,
	safety: null,
};
const outcome = (extra) => ({ ...OUTCOME, ...extra });

const EXPIRED_TEXT = "This confirmation expired. Tell me the action again to retry it.";
const OTHER_TAB_TEXT =
	"Couldn't confirm. It may have been handled in another tab. Refresh, or ask me to try again.";

// The words for a refusal that none of the branches above knows how to settle: the card is kept
// (nothing ran) and says what the server said.
function keptOutcome(reply) {
	const message = chatRefusalMessage(reply);
	return outcome({ fate: "kept", message, cardError: { message }, toast: true });
}

function storageOutcome(reply) {
	return outcome({
		fate: "kept",
		message: reply.error.message,
		cardError: reply.error,
		toast: true,
	});
}

function safetyOutcome(reply) {
	return outcome({
		safety: { message: (reply.error && reply.error.message) || REFUSALS.safety_update },
	});
}

// confirm_tool and approve_and_run (they answer in the same envelopes).
//   draft    the card is a server draft card: a stale record says "after the draft was made"
//   expired  the card's own wall-clock expiry has passed (only a bare "no longer valid" reads it)
export function confirmReplyOutcome(reply, { draft = false, expired = false } = {}) {
	if (!reply || reply.ok !== false) {
		const queued =
			reply && reply.queued
				? {
						run_id: reply.run_id,
						message_id: reply.message_id,
						position: reply.queued_position || null,
				  }
				: null;
		return outcome({ reload: true, queued });
	}
	// An agent safety update cancelled it: terminal, before any retryable branch.
	if (reply.reason_code === "safety_update") return safetyOutcome(reply);
	if (confirmationStorageUnavailable(reply)) return storageOutcome(reply);
	// Not settled (busy, already running, identity refused, a stopping armed run): keep it.
	if (keepsChatCard(reply)) return keptOutcome(reply);
	// Token gone, expired or used: the card is spent. A settled card with a specific reason_code
	// says why; only a bare legacy token (no reason_code at all) gets the opaque guess.
	if (reply.error && reply.error.type === "InvalidConfirmation") {
		const message =
			chatSettledReason(reply, { draft }) || (expired ? EXPIRED_TEXT : OTHER_TAB_TEXT);
		return outcome({ message, toast: true });
	}
	// The token was consumed and the tool failed: a durable "failed" receipt is in the thread.
	return outcome({ reload: true });
}

// dismiss_tool. A discard of an already spent card answers ok (already_handled), so the only
// refusals are the storage trouble, a card still held (busy, executing) and a safety update.
export function dismissReplyOutcome(reply) {
	if (reply && reply.reason_code === "safety_update") return safetyOutcome(reply);
	if (reply && reply.ok === false && confirmationStorageUnavailable(reply))
		return storageOutcome(reply);
	if (keepsChatCard(reply)) return keptOutcome(reply);
	return outcome({ reload: true });
}

// A call that threw (offline, a server error): the card's error object. What the server said, an
// expired session and a refused permission stay plain messages; anything else is internal (it
// carries InternalError so the desktop's card offers Report error) and says the door's fallback.
//   door  "confirm", "approve" (Approve & run) or "dismiss"
const THROWN_FALLBACK = {
	confirm: "Could not confirm this action.",
	approve: "Could not start the run.",
	dismiss: "Could not discard this action.",
};
export function thrownCardError(e, door) {
	return thrownActionError(e, THROWN_FALLBACK[door]);
}
