// Which reply each File Box wait sits under. A wait (a board question, a held
// record, an approval sheet) is filed during or at the end of the turn whose
// reply row already exists (turn_handler creates the assistant placeholder
// before any tool runs), so its creation falls between that reply's creation
// and the next message's. The chat shows each wait right after the reply that
// produced it instead of grouping them all at the bottom of the thread
// (Aerele-RnD/jarvis-admin-v2#763). A wait with no reply before it (filed before
// any reply, or whose reply is not loaded) keeps the old place at the bottom.

// Server datetimes compare as text once both carry the same shape:
// "YYYY-MM-DD HH:MM:SS[.ffffff]" (Frappe serialises a datetime with str(), which
// drops a zero microsecond part).
const STAMP_RE = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?/;
export function waitStamp(value) {
	const m = STAMP_RE.exec(String(value == null ? "" : value).trim());
	if (!m) return null;
	return `${m[1]} ${m[2]}.${(m[3] || "").padEnd(6, "0")}`;
}

/**
 * Group waits by the reply they follow.
 * @param {Array<{creation?: string}>} items the chat's visible waits
 * @param {Array<{name: string, role: string, creation?: string}>} messages the thread, in order
 * @returns {{byMessage: Record<string, object[]>, rest: object[]}} `byMessage` maps a
 *   reply's name to the waits shown under it; `rest` keeps the waits with no reply to follow.
 */
export function anchorFileboxWaits(items, messages) {
	const replies = [];
	for (const m of messages || []) {
		const at = m && m.role === "assistant" ? waitStamp(m.creation) : null;
		if (at) replies.push({ name: m.name, at });
	}
	const byMessage = {};
	const rest = [];
	for (const item of items || []) {
		const at = waitStamp(item && item.creation);
		let anchor = null;
		if (at) for (const r of replies) if (r.at <= at) anchor = r.name;
		if (anchor == null) rest.push(item);
		else (byMessage[anchor] ||= []).push(item);
	}
	return { byMessage, rest };
}
