// One card at the tail. The newest unsaved draft card is drawn under the last assistant
// message, where the last assistant message may also carry its own question card
// (jarvis-ask) or legacy confirm strip. Only one shows:
// - a draft block in the last message itself wins, exactly as it always has;
// - a kept draft from an earlier message gives way to the last message's own card until
//   it is answered (the answer lands a new message and the rule runs again);
// - confirm comes before ask, the order the thread draws them in.
// Pure, relative imports only.
export function tailCard({ hasDraft, draftIsTail, tailHasAsk, tailHasConfirm }) {
	if (hasDraft && (draftIsTail || !(tailHasAsk || tailHasConfirm))) return "draft";
	if (tailHasConfirm) return "confirm";
	if (tailHasAsk) return "ask";
	return "none";
}
