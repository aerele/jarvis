// Settings -> "Delete all chat history". The server keeps a chat that is waiting
// for a reply (deleting it would take a live turn from under the relay) and
// answers { ok, deleted, skipped, kept: [names], failed, failed_names: [names] }:
// `skipped`/`kept` are the chats kept for their reply, `failed`/`failed_names` the
// ones an error left undeleted. What the user is told, and whether the chat they
// have open stays open, is decided here so it can be tested without mounting
// ChatView. An older server answers without these: that reads as "nothing was
// kept, nothing failed", which is what it did.

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

// A chat the server could not delete (an error, not a reply in progress): waiting
// does not help, trying again may.
export function failedChatsNotice(failed) {
	if (!(failed > 0)) return "";
	return failed === 1
		? "1 chat could not be deleted. Try Delete all again."
		: `${failed} chats could not be deleted. Try Delete all again.`;
}

// The server deletes chat by chat, so a request that failed may have deleted some.
export function clearHistoryFailedNotice(detail) {
	const what = String(detail || "").trim() || "Could not delete history";
	return `${what.replace(/[.\s]+$/, "")}. Some chats may already have been deleted.`;
}

function countOf(count, names) {
	const n = Number(count);
	return Number.isFinite(n) && n > 0 ? n : names.length;
}

// -> { notice, failed, openChatKept }: the line to show ("" for none), whether any
// chat could not be deleted, and whether the conversation open in the view
// (`openId`) is still there, kept for its reply or left by a failure.
export function clearHistoryOutcome(res, openId) {
	const keptNames = Array.isArray(res?.kept) ? res.kept : [];
	const failedNames = Array.isArray(res?.failed_names) ? res.failed_names : [];
	const kept = countOf(res?.skipped, keptNames);
	const failed = countOf(res?.failed, failedNames);
	const deleted = res?.deleted == null ? NaN : Number(res.deleted);
	const keptLine = keptChatsNotice(kept, deleted);
	const failedLine = failedChatsNotice(failed);
	return {
		notice:
			keptLine && failedLine
				? `${keptLine} ${failedLine}`
				: keptLine || (failedLine && deletedChatsLine(deleted) + failedLine),
		failed: failed > 0,
		openChatKept:
			Boolean(openId) && (keptNames.includes(openId) || failedNames.includes(openId)),
	};
}
