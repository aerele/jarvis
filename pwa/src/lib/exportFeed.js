// Which background-export events the phone tells the user about. Kept free of app imports so
// node can test it. The desktop applies the same rule in frontend/src/notify/globalNotifier.js.
//
// Only an outcome counts. A queued retry, a pause (it restarts by itself), the user's own cancel,
// a quiet expiry and the card's first announcement ("initial") stay silent.
const OUTCOMES = new Set(["ready", "too large", "too slow", "failed"]);

export function exportSignals(e) {
	return (
		e?.kind === "export:changed" &&
		!!(e.conversation_id || e.conversation) &&
		!e.initial &&
		OUTCOMES.has(e.status)
	);
}

/** The bell-feed row for an export event, or null when it is not worth one. */
export function exportFeedEntry(e, at) {
	if (!exportSignals(e)) return null;
	const conversation = e.conversation_id || e.conversation;
	return {
		id: `exp:${e.message_id || conversation}`,
		kind: "export-finished",
		title: e.status === "ready" ? "Export ready" : "Export stopped",
		body: e.title || "Your export changed state.",
		conversation,
		at,
		read: false,
	};
}
