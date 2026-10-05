// The Jarvis Admin's view of every user's macros (Settings, Administration,
// Macros). `src/api.js` is frozen: new endpoints get thin wrappers in per-feature
// modules under src/api/.
//
// The server gates each of these on the Jarvis Admin role and accepts POST only
// (frappe-ui's call() always posts). The client's own gate hides the pane; it is
// not what protects the data.
import { call } from "frappe-ui";

const MA = "jarvis.chat.macros_admin_api.";

// One page of every user's macros, with no step or summary text.
// `filters`: { owner, armed, scheduled, live_run }; an empty value is "not filtering".
// `search` matches the macro's name, its owner's address and its owner's full name.
// -> { rows, total, has_more, start, page_length }
// Each row: name, macro_name, owner, owner_full_name, enabled, skip_confirmation,
// the schedule fields, next_run_at, next_run_is_retry, last_run ({ status, trigger,
// started_at, finished_at, stopped_by_admin }) and live_run (the run Stop acts on,
// or ""). `stopped_by_admin` is all the list says about why a run ended: the reason
// itself is shown with the opened macro.
export const adminListMacros = ({ search = "", filters = {}, start = 0, pageLength = 20 } = {}) =>
	call(MA + "admin_list_macros", {
		search,
		filters: JSON.stringify(filters || {}),
		start,
		page_length: pageLength,
	});

// The owner filter's choices, asked for once (not with every page of the list).
// -> { owners: [{ user, full_name, macros }], more }; `more`: the site has more
// owners than are listed, and the search box reaches the rest.
export const adminMacroOwners = () => call(MA + "admin_macro_owners");

// One macro to look at: its settings, steps, summarized prompt and recent runs
// (status, trigger, times, step counts, the reason each ended). Never a run's chat.
export const adminGetMacro = (name) => call(MA + "admin_get_macro", { name });

// Stop a live run of any user's macro.
// -> { ok, stopped, status, message }; `stopped` is false when the run had already
// ended, and `message` says so.
export const adminStopRun = (run) => call(MA + "admin_stop_run", { run });

// Put another user's macro on hold: it does not run at all (by hand, on its
// schedule, or a parked run resuming) until an admin releases it. The hold also
// switches the macro off, unschedules and disarms it, and stops its live runs.
// `reason` is required and is shown to the owner on their macro form.
// Refused on the admin's own macro.
// -> { ok, held, stopped_runs }
export const adminHold = (macro, reason) => call(MA + "admin_hold", { macro, reason });

// Lift the hold. Nothing turns itself back on: the owner does that.
// -> { ok, released }; `released` is false when the macro was not on hold.
export const adminRelease = (macro) => call(MA + "admin_release", { macro });

// Delete any user's macro, through the owner's own delete: its live runs are
// stopped first. The owner's Macros list shows them a notice that it happened.
// -> { ok, deleted, stopped_runs }
export const adminDelete = (macro) => call(MA + "admin_delete", { macro });

// Hand another user's macro to `newOwner` (a user's name). It arrives switched off,
// unscheduled and disarmed, with its summary, its steps' skills, its shares and
// its assignments cleared; its live runs are stopped first. Both owners are told.
// Refused on the admin's own macro.
// -> { ok, handed_over, new_owner, stopped_runs }
export const adminHandover = (macro, newOwner) =>
	call(MA + "admin_handover", { macro, new_owner: newOwner });

// Who a macro can be handed to: enabled users with Jarvis access matching `search`
// (address or full name), at most 20.
// -> { users: [{ user, full_name, macros }], max }; `max` is how many macros one
// user can have.
export const adminHandoverTargets = (search = "") =>
	call(MA + "admin_handover_targets", { search });
