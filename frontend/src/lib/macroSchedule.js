// frontend/src/lib/macroSchedule.js
// Pure decisions about how a macro's schedule is shown. Dependency-free so
// `node --test` can hold them: both are silent when wrong (a list cell that reads
// as healthy while the macro is not going to run).

// How long a slot may sit past its time before the list calls it overdue. Longer
// than an hour on purpose: until the five-minute sweep is live on a site (it needs a
// migrate) the sweep is hourly, and a 10:15 slot legitimately waits until 11:00. A
// slot that failed has its retry time written to `next_run_at`, so it is not "past"
// either. What is left past this mark is a slot nothing is coming for.
export const OVERDUE_AFTER_MS = 75 * 60 * 1000;

// Which state a macro's next slot is in.
//   none     - no schedule, or no readable slot
//   off      - the macro is turned off: the scheduler skips its slots, recording nothing
//   upcoming - a future slot
//   due      - the slot has passed and the next sweep should pick it up
//   overdue  - the slot passed long enough ago that no sweep is going to
export function nextRunState({ scheduleEnabled, enabled, nextRunMs, nowMs }) {
	if (!scheduleEnabled || !Number.isFinite(nextRunMs)) return { kind: "none" };
	if (!enabled) return { kind: "off" };
	const lateMs = nowMs - nextRunMs;
	if (lateMs < 0) return { kind: "upcoming" };
	return { kind: lateMs < OVERDUE_AFTER_MS ? "due" : "overdue" };
}

// What the list's "Next run" cell shows: { text, hint, tone }, or null for the "-"
// placeholder. The cell used to print the bare relative time, so a slot waiting for
// the sweep read "Next run: 5 minutes ago". `when` is the slot's exact date and
// `relative` its relative time, both already formatted by the caller.
export function nextRunCell({ scheduleEnabled, enabled, nextRunMs, nowMs, when, relative }) {
	const { kind } = nextRunState({ scheduleEnabled, enabled, nextRunMs, nowMs });
	if (kind === "upcoming") return { text: relative, hint: when, tone: "" };
	if (kind === "due")
		return { text: "Due now", hint: `Was due ${when}. It should start shortly.`, tone: "" };
	if (kind === "overdue")
		return {
			text: "Overdue",
			hint:
				`Was due ${when} and has not started. Check the Runs tab for a failed ` +
				"attempt. If there is none, scheduled jobs may not be running on this site.",
			tone: "warn",
		};
	if (kind === "off")
		return {
			text: "Turned off",
			hint: "This macro is turned off, so its scheduled runs are skipped. Turn it on to resume.",
			tone: "muted",
		};
	return null;
}

// Why the logged-in account cannot schedule a NEW macro, or "" when it can. A new
// macro has no owner yet, so there is nothing for the server to judge; it will be
// owned by whoever saves it. For a saved macro the form uses the server's own
// `schedule_blocked_reason` (macros_api._schedule_block_reason) instead, which is
// judged from the macro's owner. The server refuses the save either way.
export function cannotScheduleReason(user) {
	if (user !== "Administrator") return "";
	return "Scheduled macros cannot run as Administrator. Sign in as a named user to schedule a macro.";
}
