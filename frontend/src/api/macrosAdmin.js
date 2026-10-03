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
// -> { rows, total, has_more, start, page_length, owners: [{ user, full_name, macros }] }
// Each row: name, macro_name, owner, owner_full_name, enabled, skip_confirmation,
// the schedule fields, next_run_at, next_run_is_retry, last_run ({ status, trigger,
// started_at, finished_at }) and live_run (the run Stop acts on, or "").
export const adminListMacros = ({ search = "", filters = {}, start = 0, pageLength = 20 } = {}) =>
	call(MA + "admin_list_macros", {
		search,
		filters: JSON.stringify(filters || {}),
		start,
		page_length: pageLength,
	});

// One macro to look at: its settings, steps, summarized prompt and recent runs
// (status, trigger, times, step counts, the reason each ended). Never a run's chat.
export const adminGetMacro = (name) => call(MA + "admin_get_macro", { name });

// Stop a live run of any user's macro.
// -> { ok, stopped, status, message }; `stopped` is false when the run had already
// ended, and `message` says so.
export const adminStopRun = (run) => call(MA + "admin_stop_run", { run });
