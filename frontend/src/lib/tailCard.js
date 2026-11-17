// One card at the tail. The newest unsaved draft card is drawn under the last assistant
// message, where the last assistant message may also carry its own question card
// (jarvis-ask) or legacy confirm strip. Only one shows:
// - a draft block in the last message itself wins, exactly as it always has;
// - any other action block of its own in the last message (an email draft, a gated verb's
//   legacy note, an unknown verb's error) wins over a kept draft, as before;
// - a kept draft from an earlier message gives way to the last message's own card until
//   it is answered (the answer lands a new message and the rule runs again);
// - confirm comes before ask, the order the thread draws them in.
// Pure, relative imports only.
export function tailCard({ hasDraft, draftIsTail, tailHasAsk, tailHasConfirm, tailHasOwnAction }) {
	if (hasDraft && draftIsTail) return "draft";
	if (tailHasOwnAction) return "own";
	if (hasDraft && !(tailHasAsk || tailHasConfirm)) return "draft";
	if (tailHasConfirm) return "confirm";
	if (tailHasAsk) return "ask";
	return "none";
}
