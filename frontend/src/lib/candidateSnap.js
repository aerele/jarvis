// Pure decision behind ChatView.vue's snapCandidateInto (review C3, plan
// .claude/workflow/plans/2026-09-28-live-turn-steps.md): does a still-typing
// narration candidate (liveCandidate — splitNarration's "this might still be
// a step" half, @/lib/liveTurn) belong to the message about to be flushed,
// and if so, what full text should be fed back into the revealer BEFORE that
// flush snaps to its stored target.
//
// Why this exists at all: assistant:delta only ever paces the ANSWER half of
// splitNarration into the revealer, so its target stays "" for as long as the
// shown text still looks like a step (<=320 chars, one line, no markdown
// block). run:end already re-feeds the candidate's full text before its own
// flush; stop and error must do the exact same thing or flushReveal(id) snaps
// to "" and the bubble the user just watched stream in goes blank.
//
// Matched primarily by msgId — the row identity every caller actually has to
// hand (stopRun's found row, a terminal's p.message_id) — falling back to the
// run only when msgId was never set (defensive; every liveCandidate write
// site sets both together, see ChatView.vue's assistant:delta).
//
// Not part of liveTurn.js on purpose (kept out of that module's own surface);
// tiny enough to live here instead of inlined three times in ChatView.vue.

/**
 * @param {{msgId?: string|null, runId?: string|null, full?: string}|null|undefined} candidate liveCandidate.value
 * @param {string|null|undefined} messageId the row about to be flushed
 * @param {string|null|undefined} currentRunId ChatView's currentRunId.value
 * @returns {string|null} the text to feed the revealer, or null when nothing applies
 */
export function candidateTextFor(candidate, messageId, currentRunId) {
	if (!messageId || !candidate || !candidate.full) return null;
	const belongs = candidate.msgId
		? candidate.msgId === messageId
		: candidate.runId === currentRunId;
	return belongs ? candidate.full : null;
}
