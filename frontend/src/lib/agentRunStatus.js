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

/**
 * jarvis#1062 P1-7 (production-readiness audit): a failed run and a stopped
 * run both showed "0 findings" in the runs rail, with nothing distinguishing
 * one from the other short of opening the run. A short, row-level reason:
 * the first 60 chars of the recorded error for a failed run (truncated with
 * an ellipsis, never the full trace - that lives in FindingsPanel's own
 * failed-run banner), or the fixed "Stopped by operator." for a stopped run
 * (an operator action, not a failure - no error text implied or needed).
 * "" for every other status, so the row's v-if simply omits the line.
 */
export function runReason(row) {
	if (!row) return "";
	if (row.status === "failed") {
		const err = String(row.error || "").trim();
		if (!err) return "This run failed.";
		return err.length > 60 ? err.slice(0, 60).trimEnd() + "…" : err;
	}
	if (row.status === "stopped") return "Stopped by operator.";
	return "";
}

/**
 * Did this run leave coverage gaps the viewer must be warned about? A run that
 * finished with only missing-data gaps now reads status "completed" (its lifecycle
 * finished) yet still carries a coverage_note — so the "not clean" cue keys off the
 * NOTE, not the status, and stays visible on a green "completed" row. Centralised so the
 * runs board triangle and the FindingsPanel banner ask the same question (no drift).
 * A failed run owns its own red banner, so it is excluded here; a stopped run with a
 * coverage_note is intentionally included (it too is unreviewed, and reads neither green
 * nor all-clear).
 */
export function coverageWarned(row) {
	return !!(row && row.coverage_note) && row.status !== "failed";
}
