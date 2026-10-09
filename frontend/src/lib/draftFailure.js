// A failed draft-panel save (round 2, R2-9), read from apply_action's envelope.
//
// A fixable failure keeps the panel open with the problem fields marked, and the
// user fixes it there. A failure no value in the panel fixes comes back with
// `closed: true`: the panel closes and the assistant explains in the chat (`note`
// says so where the person clicked). `assistant_told: false` means that explanation
// could not be sent, so the note says to ask in the chat instead of promising a
// reply. `outcome: "partial"` means something was saved before the failure, so the
// words never say nothing was saved. Shared by the desktop panel and the phone card
// (@shared/lib/draftFailure.js).
import { personError } from "./actionSummary.js";

export const FAILURE_FALLBACK = "Could not save. Check the values.";

// { closed, partial, told, error, note } for a failed apply, else null. `agent` is
// the assistant's (whitelabel) name.
export function draftFailureOf(r, agent = "Jarvis") {
	if (!r || r.ok !== false) return null;
	const err = personError(r.error || {});
	const message = (typeof err.message === "string" && err.message.trim()) || FAILURE_FALLBACK;
	// The notes add a sentence after it: end it as one.
	const sentence = /[.!?]$/.test(message) ? message : `${message}.`;
	const closed = !!r.closed;
	const partial = r.outcome === "partial";
	const told = r.assistant_told !== false;
	let note = "";
	if (partial) {
		note = `Part of this may already be saved: ${sentence} Check the record before trying again.`;
	} else if (closed) {
		note = `Couldn't save this: ${sentence}`;
		if (told) note += " The reply in the chat explains what to do.";
	}
	if (closed && !told) note += ` ${agent} could not be told about it, so ask in the chat.`;
	return { closed, partial, told, error: { ...err, message }, note };
}
