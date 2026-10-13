// History paging (#715 F13). An open chat holds the newest page of its transcript
// plus whatever older pages the reader scrolled up to load. A resync (a turn
// settling, a reconnect, a card applied) re-reads only the newest page and keeps
// the older rows it already holds. Shared by the desktop chat and the PWA.
//
// Server contract (jarvis.chat.api.get_conversation with `limit`): `messages` is one
// page, oldest first, opening on a user row; `paging` is {limit, before, has_more},
// where `before` is the cursor for the next older page; `older_pending` holds the
// pending cards older than the newest page; `refreshed` is the current state of
// the held rows a call named in `refresh`.

// Rows per page. A page widens back to the start of its oldest turn (by at most 200
// rows, HISTORY_TURN_WIDEN_MAX on the server), so a reply never loads without its
// tool rows and run state.
export const HISTORY_PAGE_SIZE = 80;

// Most held rows one resync asks the server to refresh (its cap too).
const REFRESH_MAX = 50;

// {hasMore, before, newest} from a response: whether an older page exists, its
// cursor, and the newest seq this page holds (what a later resync must reach back
// to for the rows in between to be on screen). A whole-thread (unpaged) answer
// has no older page.
export function pagingOf(d) {
	const p = d && d.paging;
	const page = (d && d.messages) || [];
	const last = page.length ? page[page.length - 1].seq : null;
	const newest = typeof last === "number" ? last : null;
	if (!p || !p.has_more || p.before == null) return { hasMore: false, before: null, newest };
	return { hasMore: true, before: p.before, newest };
}

// Held rows whose state can change while the reader looks elsewhere: pending cards,
// which resolve into receipts. A resync names them so the server sends them fresh.
export function refreshNames(held) {
	return (held || [])
		.filter((m) => m && m.role === "tool" && m.tool_status === "pending" && m.name)
		.map((m) => m.name)
		.slice(0, REFRESH_MAX);
}

// The rows to seed confirm cards from after a load: the page itself plus every
// still-pending card older than it, whether or not that part of the thread is loaded.
export function pendingSeedRows(d, messages) {
	return [...((d && d.older_pending) || []), ...(messages || [])];
}

// Merge a freshly read newest page into the rows on screen. `heldPaging` is the
// state the held rows were loaded with; `asked` is what the call passed as
// `refresh`. Returns {messages, paging}.
//
// When the page reaches the first row of the thread (no older page), it IS the
// thread: nothing held survives, so a cleared or rebuilt chat cannot keep ghosts.
// When the page starts after the newest row the last page held (more arrived in
// between than one page covers), keeping the held rows would leave a hole in the
// middle of the thread, so the page starts over. Otherwise the held rows older
// than the page stay (only server rows, by seq), each replaced by its refreshed
// copy when the server sent one, and dropped when it was asked for but not
// returned (deleted or hidden). Holding older rows keeps the cursor the reader
// already reached; with none held, the page sets it.
export function mergeNewestPage(held, heldPaging, d, asked = []) {
	const page = (d && d.messages) || [];
	const fresh = pagingOf(d);
	const start = page.length ? page[0].seq : null;
	if (!fresh.hasMore || typeof start !== "number") return { messages: page, paging: fresh };
	const heldNewest = heldPaging && heldPaging.newest;
	if (typeof heldNewest !== "number" || heldNewest < start)
		return { messages: page, paging: fresh };
	const byName = new Map();
	for (const m of [...((d && d.older_pending) || []), ...((d && d.refreshed) || [])])
		byName.set(m.name, m);
	const askedSet = new Set(asked);
	const kept = (held || [])
		.filter((m) => m && typeof m.seq === "number" && m.seq < start)
		.filter((m) => !askedSet.has(m.name) || byName.has(m.name))
		.map((m) => byName.get(m.name) || m);
	if (!kept.length) return { messages: page, paging: fresh };
	return { messages: [...kept, ...page], paging: { ...heldPaging, newest: fresh.newest } };
}

// Put an older page above the rows on screen (no duplicates). The cursor moves to
// the older page's; the newest seq held stays. Returns {messages, paging}.
export function prependOlderPage(held, heldPaging, d) {
	const rows = held || [];
	const names = new Set(rows.map((m) => m && m.name));
	const older = ((d && d.messages) || []).filter((m) => !names.has(m.name));
	const { hasMore, before } = pagingOf(d);
	const newest = heldPaging && typeof heldPaging.newest === "number" ? heldPaging.newest : null;
	return { messages: [...older, ...rows], paging: { hasMore, before, newest } };
}

// A tool row as the Activity view shows it: whole once its detail has been fetched
// (get_tool_detail), else as the page sent it.
export function withToolDetail(row, details) {
	const full = row && details && details[row.name];
	return full ? { ...row, tool_args: full.tool_args, tool_result: full.tool_result } : row;
}

// Whether expanding this tool row needs its detail fetched first.
export function needsToolDetail(row, details) {
	return !!(row && row.tool_result_compact && !(details && details[row.name]));
}
