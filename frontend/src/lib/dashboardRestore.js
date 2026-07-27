// Rehydrating the Dashboards builder canvas from a loaded transcript.
//
// The canvas html is component state on the routed builder page, so any
// navigation drops it — but nothing is actually lost: chat.api.get_conversation
// returns every message's `canvas` list ([{name, title, type, file_url}]), and
// chat.api.get_canvas can replay any past message's artifact for the owner of
// the conversation. So "restore the canvas" is just: pull that message's
// artifact again. No new server state, no draft row, no extra endpoint.
//
// WHICH message, though, is not "the newest html in the conversation": the
// builder's thread is an ordinary Jarvis Conversation that the user can also
// open in main chat ("Open in chat"), where an html artifact means something
// else entirely. Restoring that would put a chat answer on the builder canvas
// and arm "Save dashboard" over it. So the builder remembers the message id of
// the canvas IT last rendered (sticky, per user) and restores exactly that one.

/**
 * The canvas frame for a specific message of a transcript, in the shape the
 * realtime `kind:"canvas"` frame uses — so the page's existing onCanvas handler
 * consumes a replay and a live frame through one path.
 *
 * @param {Array<{name?: string, canvas?: Array<{type?: string}>}>} messages
 *        get_conversation's message rows (canvas already parsed into a list)
 * @param {string} messageId the builder's last rendered canvas message
 * @returns {{message_id: string, items: Array<object>}|null}
 */
export function builderCanvasFrame(messages, messageId) {
	if (!messageId) return null;
	const rows = Array.isArray(messages) ? messages : [];
	for (let i = rows.length - 1; i >= 0; i--) {
		const m = rows[i];
		if (!m || m.name !== messageId || !Array.isArray(m.canvas)) continue;
		// Only an html artifact can be rendered on the canvas; a message whose
		// canvas holds just an image/csv is not the frame we want to restore.
		if (!m.canvas.some((it) => it && it.type === "html")) return null;
		return { message_id: m.name, items: m.canvas };
	}
	return null;
}
