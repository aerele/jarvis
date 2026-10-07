// The reply's hover bar: its time shows from the first streamed words, its
// copy button only once the text is final (copying half a reply is wrong).
// The bar is rendered from the first delta so its height never shifts at run:end.

/** What the bar shows for message `m`. */
export function replyBarParts(m) {
	const showBar = !!m && !m.error && !!m.content;
	return { showBar, showCopy: showBar && !m.streaming };
}

/** Stamp the client time of the latest delta unless the server already has one. */
export function stampDeltaTime(m, now = Date.now()) {
	if (!m.modified && !m.creation) m.creation_browser = now;
}
