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
// formatted by the caller. `legacy` is what the cell showed before the server sent
// `last_run` (the scheduler's `last_run_at`, as { when, relative }); it is used only
// when `lastRun` is undefined, i.e. an older server during a deploy.
export function lastRunCell({ lastRun, when, relative, legacy }) {
	if (lastRun === undefined)
		return legacy && legacy.relative
			? { text: legacy.relative, hint: legacy.when, tone: "" }
			: null;
	if (!lastRun || !lastRun.status) return null;
	const reason = lastRun.error || "";
	const withReason = [when, reason].filter(Boolean).join(". ");
	switch (lastRun.status) {
		case "completed":
			// "Completed" only when there is no time to show: an empty cell reads as
			// "never ran".
			return { text: relative || "Completed", hint: withReason, tone: "" };
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

// The form's notice about the last run: { title, text, tone }, or null when there
// is nothing the owner needs to be told (it worked, it is still going, they stopped
// it). `title` says what happened in a few words; `text` is the reason, which can
// be several sentences and reads better as the lighter second line.
export function lastRunLine(lastRun) {
	if (!lastRun) return null;
	const reason = lastRun.error || "";
	if (lastRun.status === "failed")
		return { title: "The last run failed", text: reason, tone: "bad" };
	if (!reason) return null;
	if (lastRun.status === "stopped")
		return { title: "The last run stopped before it finished", text: reason, tone: "warn" };
	if (lastRun.status === "completed")
		return { title: "The last run finished, with a note", text: reason, tone: "" };
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

// A run that has not ended, as the server counts it (macros._LIVE_RUN_STATUSES):
// in progress, or parked until the site has capacity for its next step.
const LIVE_RUN = ["running", "waiting_capacity"];

// What a delete confirmation adds when the page knows a macro about to be deleted
// is running: the server stops the run first, and the user should hear that before
// they confirm, not after. `lastRuns` is the `last_run` of each macro being deleted
// (one for the form, the selection for the list). "" when none is live.
export function deleteWarning(lastRuns) {
	const live = (lastRuns || []).filter((r) => r && LIVE_RUN.includes(r.status)).length;
	if (!live) return "";
	if ((lastRuns || []).length === 1) return "This macro is running. Deleting it stops the run.";
	if (live === 1) return "One of these macros is running. Deleting it stops the run.";
	return `${live} of these macros are running. Deleting them stops their runs.`;
}

// What the success toast adds after a delete that stopped runs. `res` is the
// server's answer (`stopped_runs`); "" when it stopped none, and for a server from
// before it reported the count. It is appended to a toast that has no full stop
// ("Macro deleted"), so it brings its own.
export function stoppedRunsNote(res) {
	const n = Number(res && res.stopped_runs);
	if (!(n > 0)) return "";
	return n === 1 ? ". 1 run was stopped." : `. ${n} runs were stopped.`;
}
