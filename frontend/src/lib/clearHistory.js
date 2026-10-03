// Settings -> "Delete all chat history". The server keeps a chat that is waiting
// for a reply (deleting it would take a live turn from under the relay) and
// answers { ok, deleted, skipped, kept: [names] }. What the user is told, and
// whether the chat they have open stays open, is decided here so it can be
// tested without mounting ChatView. An older server answers without `skipped`
// and `kept`: that reads as "nothing was kept", which is what it did.

export const CLEAR_HISTORY_CONFIRM =
	"Every conversation and message will be permanently deleted. A chat with a reply still in progress is kept. Macros, skills and settings stay. This can't be undone.";

export function keptChatsNotice(kept) {
	if (!(kept > 0)) return "";
	return kept === 1
		? "1 chat was kept because a reply is still in progress. Delete again once it finishes."
		: `${kept} chats were kept because a reply is still in progress. Delete again once they finish.`;
}

// -> { notice, openChatKept }: the line to show ("" for none), and whether the
// conversation open in the view (`openId`) is one of the kept ones.
export function clearHistoryOutcome(res, openId) {
	const names = Array.isArray(res?.kept) ? res.kept : [];
	const count = Number(res?.skipped);
	const kept = Number.isFinite(count) && count > 0 ? count : names.length;
	return {
		notice: keptChatsNotice(kept),
		openChatKept: Boolean(openId) && names.includes(openId),
	};
}
