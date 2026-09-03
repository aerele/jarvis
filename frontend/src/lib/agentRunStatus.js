/**
 * The Jarvis Agent Run lifecycle status -> frappe-ui Badge theme, in one
 * place so every surface that shows a run's status (AgentRunsBoard's rail,
 * AgentActivityTab's feed) reads the same colour for the same state.
 *
 * Split out of AgentRunsBoard.vue (jarvis#1062 owner feedback): the
 * Activity feed's run rows need the SAME theme, not a second copy that can
 * drift (e.g. "stopped" forgotten and falling back to the wrong grey).
 */
export const STATUS_THEME = {
	running: "blue",
	completed: "green",
	partial: "orange",
	failed: "red",
	stopped: "gray",
};
