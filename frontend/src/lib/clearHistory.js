// Settings -> "Delete all chat history". The server keeps a chat that is waiting
// for a reply (deleting it would take a live turn from under the relay) and
// answers { ok, deleted, skipped, kept: [names] }. What the user is told, and
// whether the chat they have open stays open, is decided here so it can be
// tested without mounting ChatView. An older server answers without `skipped`
// and `kept`: that reads as "nothing was kept", which is what it did.

export const CLEAR_HISTORY_CONFIRM =
	"Every conversation and message will be permanently deleted. A chat with a reply still in progress is kept. Macros, skills and settings stay. This can't be undone.";

function deletedChatsLine(deleted) {
	if (!Number.isFinite(deleted) || deleted < 0) return "";
	if (deleted === 0) return "No chats were deleted. ";
	return deleted === 1 ? "Deleted 1 chat. " : `Deleted ${deleted} chats. `;
}

// What went, what stayed, and what to do about what stayed. `deleted` is left out
// of the line when the server did not say.
export function keptChatsNotice(kept, deleted) {
	if (!(kept > 0)) return "";
	const stayed =
		kept === 1
			? "1 chat was kept because a reply is still in progress. Delete all again once the reply finishes."
			: `${kept} chats were kept because their replies are still in progress. Delete all again once the replies finish.`;
	return deletedChatsLine(deleted) + stayed;
}

// The server deletes chat by chat, so a request that failed may have deleted some.
export function clearHistoryFailedNotice(detail) {
	const what = String(detail || "").trim() || "Could not delete history";
	return `${what.replace(/[.\s]+$/, "")}. Some chats may already have been deleted.`;
}

// -> { notice, openChatKept }: the line to show ("" for none), and whether the
// conversation open in the view (`openId`) is one of the kept ones.
export function clearHistoryOutcome(res, openId) {
	const names = Array.isArray(res?.kept) ? res.kept : [];
	const count = Number(res?.skipped);
	const kept = Number.isFinite(count) && count > 0 ? count : names.length;
	const deleted = res?.deleted == null ? NaN : Number(res.deleted);
	return {
		notice: keptChatsNotice(kept, deleted),
		openChatKept: Boolean(openId) && names.includes(openId),
	};
}
