// When a chat that looks busy has gone quiet: the realtime stream is best-effort, so a
// run whose terminal event never arrived (a workspace that stopped responding, a dropped
// socket) used to keep the spinner and Stop button up until a reload (#590). After this
// long with no run event, ChatView re-reads the conversation from the server and stops
// spinning when nothing there is still running.
export const SILENT_RUN_RESYNC_MS = 120 * 1000;

// A row written this recently is treated as live even without a turn state.
const FRESH_ROW_MS = 5 * 60 * 1000;
// The turn states after which nothing more will happen to the reply.
const TERMINAL_TURN_STATES = new Set(["done", "errored", "cancelled"]);

export function isSilentRun(busy, lastSignalAt, now = Date.now()) {
	return !!busy && now - lastSignalAt >= SILENT_RUN_RESYNC_MS;
}

// Whether a reloaded still-streaming reply row belongs to a run that is alive. A recent
// write says so; so does the server's turn state when it has not ended (a long tool call
// writes nothing for minutes, and reopening that chat used to show it stopped, #591).
export function streamingRowIsLive(row, now = Date.now()) {
	if (!row) return false;
	if (row.turn_state && !TERMINAL_TURN_STATES.has(row.turn_state)) return true;
	const modified = Date.parse(String(row.modified || "").replace(" ", "T"));
	return Number.isFinite(modified) && now - modified < FRESH_ROW_MS;
}

// After a silent stretch and a reload: stop the spinner only when the reload found no
// live reply and the server holds no queued / starting turn for this chat.
export function shouldStopSpinning({ busy, resumed, preStream }) {
	return !!busy && !resumed && !preStream;
}
