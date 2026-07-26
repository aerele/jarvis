// "Learn from custom apps" API wrappers - thin bindings around
// `jarvis.chat.app_learning_api.*` (the api/triggers.js convention: src/api.js
// is frozen, new endpoints get per-feature modules under src/api/). Every
// endpoint is manage-gated server-side (System Manager | Jarvis Admin) and
// answers the {ok: true, data: ...} envelope, so each wrapper unwraps `.data`
// and hands the components plain payloads. The runs list speaks the frozen
// list envelope {rows, total, has_more, start, page_length}.
import { call } from "frappe-ui";

const AL = "jarvis.chat.app_learning_api.";

// {ok, data} → data (defensive: a bare payload passes through untouched)
function unwrap(res) {
	return res && typeof res === "object" && res.data !== undefined ? res.data : res;
}

// Same request-arg normalizer as api/triggers.js `_page` (search/filters/sort/
// paging; `filters` JSON-encoded so the server `frappe.parse_json`s it).
const _page = (p = {}) => ({
	search: p.search || "",
	filters: JSON.stringify(p.filters || {}),
	sort_field: p.sort_field || "",
	sort_dir: p.sort_dir || "",
	start: p.start || 0,
	page_length: p.page_length || 20,
});

// -> {active_run: {name, app, status, batches_done, batches_total} | null,
//     queued: N,
//     apps: [{app, title, installed_version, path_ok, approx_files, approx_kb,
//             last_run: {status, finished_at} | null}]}
export const getAppLearningOverview = () => call(AL + "get_app_learning_overview").then(unwrap);

// -> [{app, title, installed_version, path_ok, approx_files, approx_kb, last_run}]
// The learnable-app roster on its own (no legacy run rollup), for the Custom App
// Learning agent's per-run app selection + consent.
export const listCustomApps = () => call(AL + "list_custom_apps").then(unwrap);

// apps: array of app names (JSON-encoded, the batch_approve/deleteTriggersBulk
// idiom); when: "" = run now | "YYYY-MM-DD HH:mm:ss" (SITE timezone) = schedule
// once. consent is always 1 - this wrapper is only ever called from the
// mandatory consent dialog's confirm.
export const scheduleAppLearning = (apps, when = "") =>
	call(AL + "schedule_app_learning", {
		apps: JSON.stringify(Array.from(apps || [])),
		when: when || "",
		consent: 1,
	}).then(unwrap);

// Cancels a non-terminal (Queued/Zipping/Analyzing/Ingesting) run by name.
export const cancelAppLearningRun = (name) =>
	call(AL + "cancel_app_learning_run", { name }).then(unwrap);

// rows: {name, app, status (Queued/Zipping/Analyzing/Ingesting/Completed/
//        Failed/Cancelled), scheduled_at, started_at, finished_at,
//        conversation, batches_done, batches_total, pages_written,
//        skills_created, skills_deferred, error, requested_by, creation};
// filters: {app, status}; sortable: creation · app · status · finished_at.
export const listAppLearningRunsPage = (p) =>
	call(AL + "list_app_learning_runs_page", _page(p)).then(unwrap);
