// The admin "User activity" page's one endpoint. `src/api.js` is frozen: new endpoints get
// thin wrappers in per-feature modules under src/api/.
//
// The server gates this on Jarvis Admin / System Manager and accepts POST only (frappe-ui's
// call() always posts). The page's own gate hides the Settings entry; it is not what
// protects the data, and a non-admin who deep-links in sees the load error.
import { call } from "frappe-ui";

const METHOD = "jarvis.activity_report.get_user_activity";

// view "summary" -> { people, people_truncated, thresholds, first_event_at, logging_health,
//   stale_days, clamped, partial, excluded_days, preparing, notes }
//   `first_event_at` is { agent, app } (a datetime, or null when none was ever recorded);
//   `people_truncated` is true when the server capped the list (flagged people first);
//   `logging_health` is { warn, app_warn, rollup_lag_days, scheduler_disabled }. A server
//   that predates `people_truncated` / `app_warn` just leaves them out.
//   Each person: user, name, turns, tokens, reads, rows_read, exports, rows_exported, writes,
//   erp_deletes, refused_denied, failed, actions, flags: [{ key, label, value }].
//   Flagged people come first. `activity` / `outcome` are ignored by the summary (a summary
//   that hid rows by outcome would hide the very flags it exists to raise).
// view "events" -> { rows, next_cursor, thresholds, preparing, notes }
//   Pass the previous page's `next_cursor` as `cursor` for the next 200 rows; `notes` is
//   empty on a cursor page (the first page's still stand).
//   Each row: at (site-local datetime), actor, actor_name, activity, method, label,
//   ref_doctype, ref_name, rows, outcome, channel, code, source.
// An empty value is "not filtering" and is left out of the request; an empty `to` is
// open-ended (the server reads up to now on the site's clock).
export const getUserActivity = ({
	view = "summary",
	from,
	to,
	user,
	activity,
	outcome,
	cursor,
} = {}) => {
	const args = { view };
	if (from) args.from_date = from;
	if (to) args.to_date = to;
	if (user) args.user = user;
	if (activity) args.activity = activity;
	if (outcome) args.outcome = outcome;
	if (cursor) args.cursor = cursor;
	return call(METHOD, args);
};
