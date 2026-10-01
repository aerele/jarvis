// frontend/src/lib/macroSchedule.js
// Pure decisions about how a macro's schedule is shown. Dependency-free so
// `node --test` can hold them: both are silent when wrong (a list cell that reads
// as healthy while the macro is not going to run).

// How long a slot may sit past its time before the list stops calling it "due".
// The sweep runs every few minutes, so a slot still waiting after this has most
// likely failed and is on its hourly retry.
export const OVERDUE_AFTER_MS = 15 * 60 * 1000;

// What the list's "Next run" cell should say. The cell used to print the relative
// time as-is, so a slot waiting for the next sweep read "Next run: 5 minutes ago".
//   none     - no schedule, or no slot computed yet
//   off      - the macro is turned off: the scheduler skips its slots, recording nothing
//   upcoming - a future slot
//   due      - the slot has passed and should start on the next sweep
//   overdue  - the slot passed a while ago and has still not started
export function nextRunState({ scheduleEnabled, enabled, nextRunMs, nowMs }) {
	if (!scheduleEnabled || nextRunMs == null) return { kind: "none" };
	if (!enabled) return { kind: "off" };
	const lateMs = nowMs - nextRunMs;
	if (lateMs < 0) return { kind: "upcoming" };
	return { kind: lateMs < OVERDUE_AFTER_MS ? "due" : "overdue" };
}

// Why this account cannot put a macro on a schedule, or "" when it can. The
// scheduler never runs unattended work as Administrator, and the server refuses the
// save (macros_api._refuse_schedule_for_barred_owner); this says so before the click.
export function cannotScheduleReason(user) {
	if (user !== "Administrator") return "";
	return "Scheduled macros cannot run as Administrator. Sign in as a named user to put this macro on a schedule.";
}
