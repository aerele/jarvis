// What a phone draft card does with the server's answer to Create or Cancel. Pure: a reply in,
// what to do out. Uses only the phone's own decision helpers (keptCard.js and denyOutcome.js, the
// ones DecisionSheet uses), so a refusal means the same thing on the sheet and on the card.
//
// outcome = { gone, error, say }
//   gone  the card is spent: take it off the list and reload the chat and the pending list, so
//         the receipt line (or the card that replaced it) shows
//   error the card stays and is usable again: this line goes under it
//   say   the card is gone and no receipt tells why: this line goes to the chat's banner
import { keptCardMessage, settledReasonMessage } from "./keptCard.js";
import { denyOutcome } from "./denyOutcome.js";

// The words for a record that changed after the draft was made. The desktop says the same
// (chatCardActions.chatSettledReason with {draft: true}); a spec holds the two together.
export const DRAFT_STALE_TEXT = "This record changed after the draft was made. Nothing was saved.";

export const CREATE_FAILED_TEXT = "Couldn't run this action.";
export const CANCEL_FAILED_TEXT = "Couldn't discard this action.";

const GONE = Object.freeze({ gone: true, error: "", say: "" });

// confirm_tool's answer.
export function createOutcome(reply, { agent = "Jarvis" } = {}) {
	if (!reply || reply.ok !== false) return GONE;
	// busy, already running, identity refused, storage trouble: the card is still Pending.
	const kept = keptCardMessage(reply);
	if (kept) return { gone: false, error: kept, say: "" };
	// An agent safety update cancelled the card: the reloaded transcript carries the Done notice.
	if (reply.reason_code === "safety_update") return GONE;
	if (reply.error && reply.error.type === "InvalidConfirmation") {
		const why = reply.reason_code === "stale" ? DRAFT_STALE_TEXT : settledReasonMessage(reply);
		return {
			gone: true,
			error: "",
			say:
				why ||
				`Couldn't confirm. It may have been handled elsewhere. Refresh, or ask ${agent} to try again.`,
		};
	}
	// The token was spent and the write failed: the receipt ("Not created ...") is the reload's.
	return GONE;
}

// dismiss_tool's answer.
export function cancelOutcome(reply) {
	const out = denyOutcome(reply);
	if (out.state === "denied" || out.state === "cancelled") return GONE;
	return { gone: false, error: out.error || CANCEL_FAILED_TEXT, say: "" };
}

// A call that threw (offline, server error): the card stays and can be pressed again.
export function thrownOutcome(kind, error) {
	const fallback = kind === "cancel" ? CANCEL_FAILED_TEXT : CREATE_FAILED_TEXT;
	return { gone: false, error: (error && error.message) || fallback, say: "" };
}
