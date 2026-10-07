// The reply's hover bar: its time shows from the first streamed words, its
// copy button only once the text is final (copying half a reply is wrong).
// The bar is rendered from the first delta so its height never shifts at run:end.

/** What the bar shows for message `m`. */
export function replyBarParts(m) {
	const showBar = !!m && !m.error && !!m.content;
	return { showBar, showCopy: showBar && !m.streaming };
}

/** Stamp the client time of the latest delta (always: a resumed row has a server creation too). */
export function stampDeltaTime(m, now = Date.now()) {
	m.creation_browser = now;
}

/** While a reply streams its time is the latest delta's; the server value rules once settled. */
export function useClientStamp(m) {
	return !!m && !!m.streaming && !!m.creation_browser;
}
