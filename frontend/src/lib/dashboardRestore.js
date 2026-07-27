// Rehydrating the Dashboards builder canvas from a loaded transcript.
//
// The canvas html is component state on the routed builder page, so any
// navigation drops it — but nothing is actually lost: chat.api.get_conversation
// returns every message's `canvas` list ([{name, title, type, file_url}]), and
// chat.api.get_canvas can replay any past message's artifact for the owner of
// the conversation. So "restore the canvas" is just: find the newest message
// that drew an html artifact and pull it again. No new server state, no draft
// row, no extra endpoint.

/**
 * The newest canvas frame in a transcript, in the shape the realtime
 * `kind:"canvas"` frame uses — so the page's existing onCanvas handler can
 * consume a replay and a live frame through one path.
 *
 * @param {Array<{name?: string, canvas?: Array<{type?: string}>}>} messages
 *        get_conversation's message rows (canvas already parsed into a list)
 * @returns {{message_id: string, items: Array<object>}|null}
 */
export function lastCanvasFrame(messages) {
	const rows = Array.isArray(messages) ? messages : [];
	for (let i = rows.length - 1; i >= 0; i--) {
		const m = rows[i];
		if (!m || !m.name || !Array.isArray(m.canvas)) continue;
		// Only an html artifact can be rendered on the canvas; a message whose
		// canvas holds just an image/csv is not the frame we want to restore.
		if (!m.canvas.some((it) => it && it.type === "html")) continue;
		return { message_id: m.name, items: m.canvas };
	}
	return null;
}
