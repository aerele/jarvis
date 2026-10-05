// Which message in the thread carries the Retry control.
//
// Retry sits on a failed reply only while that reply is the LAST message: once
// anything follows it the turn is history. One row does not count as "following":
// the message the macro engine posts to close a run (macros._post_closing_message,
// "✗ Macro failed. Step 2 failed: ..."). It is an assistant row, it lands after the
// failed step's reply, and it took Retry off the very reply it explains. The bench
// marks it with ref_doctype "Jarvis Macro Run" (get_conversation sends the field),
// so it is skipped here, for the Retry control only: it is still the last message
// for every other purpose (cards, scroll, the transcript).

const MACRO_RUN = "Jarvis Macro Run";

export function isMacroClosingMessage(m) {
	return !!m && m.role === "assistant" && m.ref_doctype === MACRO_RUN;
}

// Index, in the list the thread renders, of the one message that may show Retry;
// -1 when there is none. Whether it actually shows one is the caller's question
// (the reply has to have failed in a way that can be retried).
export function retryTargetIndex(visibleMessages) {
	const rows = visibleMessages || [];
	for (let i = rows.length - 1; i >= 0; i--) {
		if (!isMacroClosingMessage(rows[i])) return i;
	}
	return -1;
}

// What the Retry control says. Under a macro's closing message a bare "Retry"
// reads as "retry the run", and it is not that: it re-runs the one failed step as
// an ordinary chat turn, the run stays ended and its later steps do not follow.
// So when the target was found by skipping a closing message, the control says
// which thing it retries. Everywhere else it is the plain word, unchanged.
export function retryLabel(visibleMessages) {
	const rows = visibleMessages || [];
	const at = retryTargetIndex(rows);
	return at >= 0 && at < rows.length - 1 ? "Retry this step" : "Retry";
}
