// frontend/src/lib/macroRunOutcome.js
// Pure decisions about how a macro run's outcome is shown. Dependency-free so they
// can be held by tests: each is silent when wrong (a list that reads as healthy
// for a macro whose last run failed).
//
// `lastRun` is the server's `last_run` on a macro (macros_api._last_runs):
// { status, error, started_at, finished_at, conversation, trigger }, or null when
// the macro never ran. `error` holds the reason for a failed or stopped run, and a
// plain note on a completed one (a step that only drafted a record).

// What the Macros list's "Last run" cell shows: { text, hint, tone }, or null for
// the "-" placeholder. A run that worked keeps showing WHEN it ran, as the cell
// always did; one that did not says so in a word, with the reason on hover.
// `when` is the run's exact date and `relative` its relative time, both already
// formatted by the caller.
export function lastRunCell({ lastRun, when, relative }) {
	if (!lastRun || !lastRun.status) return null;
	const reason = lastRun.error || "";
	const withReason = reason ? `${when}. ${reason}` : when;
	switch (lastRun.status) {
		case "completed":
			return { text: relative, hint: withReason, tone: "" };
		case "failed":
			return { text: "Failed", hint: withReason, tone: "bad" };
		case "stopped":
			// With a reason the engine stopped it (a step is waiting on the user);
			// without one the user pressed Stop, which is not worth a warning.
			return { text: "Stopped", hint: withReason, tone: reason ? "warn" : "muted" };
		case "running":
		case "queued":
			return { text: "Running", hint: `Started ${when}`, tone: "info" };
		case "waiting_capacity":
			return { text: "Waiting", hint: `Waiting for capacity since ${when}`, tone: "info" };
		default:
			// A status this bundle does not know (a newer server): show it as it is.
			// Falling back to the time would read as "it worked".
			return { text: String(lastRun.status), hint: withReason, tone: "" };
	}
}

// The form's one line about the last run: { text, tone }, or null when there is
// nothing the owner needs to be told (it worked, it is still going, they stopped it).
export function lastRunLine(lastRun) {
	if (!lastRun) return null;
	const reason = lastRun.error || "";
	if (lastRun.status === "failed")
		return {
			text: reason ? `The last run failed. ${reason}` : "The last run failed.",
			tone: "bad",
		};
	if (!reason) return null;
	if (lastRun.status === "stopped") return { text: reason, tone: "warn" };
	if (lastRun.status === "completed") return { text: reason, tone: "" };
	return null;
}

// The Runs tab's detail dialog: its title and what the stored text is called. It was
// titled "Run failed" for everything but a stopped run, including a completed one.
export function runDetail(run) {
	const status = run && run.status;
	if (status === "failed") return { title: "Run failed", label: "Error" };
	if (status === "stopped") return { title: "Run stopped", label: "Reason" };
	if (status === "completed") return { title: "Run completed", label: "Note" };
	return { title: "Run details", label: "Note" };
}

// What a `macro:done` event is worth telling the user: { title, body }, or null.
// Only a FAILED run. A run that stopped at a confirmation card was announced when
// the card parked ("needs your confirmation"), and a run the user stopped needs no
// announcement at all.
export function macroDoneSignal(p) {
	if (!p || p.status !== "failed") return null;
	return {
		title: p.macro_name ? `Macro failed: ${p.macro_name}` : "Macro failed",
		body: p.error || "Open the run to see what happened.",
	};
}
