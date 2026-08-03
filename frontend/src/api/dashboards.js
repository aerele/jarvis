// Dashboards-page API wrappers - thin bindings around `jarvis.chat.dashboards_api.*`
// (src/api.js is frozen - new endpoints get per-feature modules under src/api/,
// the api/triggers.js convention). CRUD endpoints answer with the {ok, data}
// envelope and are unwrapped here; the two data-execution endpoints
// (runDashboardSource / callDashboardTool) are passed through UN-unwrapped
// because their {ok:false, error:{code, message}} arm is load-bearing - the
// canvas maps those codes onto per-widget errors inside the iframe.
import { call } from "frappe-ui";
import { listPageArgs as _page } from "./listPageArgs";

const DB = "jarvis.chat.dashboards_api.";

// {ok, data} → data (defensive: a bare payload passes through untouched)
function unwrap(res) {
	return res && typeof res === "object" && res.data !== undefined ? res.data : res;
}

// Request args come from the ONE shared encoder (plan 08 P0-01). This wrapper's
// private `_page` used to rebuild the request WITHOUT `filters_v2`, so a Saved
// Dashboards clause silently never reached the endpoint — the exact defect the
// round-2 review found. It now routes through `listPageArgs` like every other
// migrated wrapper, so the drift cannot return.

// ── caps probe ────────────────────────────────────────────────────────────────
// -> {creatable_scopes: ["User", ...], manageable_roles: [...], max_sources,
//     max_html_chars, max_rows}
export const getDashboardsCaps = () => call(DB + "get_dashboards_caps").then(unwrap);

// ── dashboards ────────────────────────────────────────────────────────────────
// rows: {name, dashboard_title, description, dashboard_type, scope, target_role,
//        target_user, owner, modified}. Allowed filter keys ONLY: scope,
// dashboard_type, owner - unknown keys throw, so callers peel `search` out of
// filters before calling (the MacrosList idiom).
export const listDashboardsPage = (p) => call(DB + "list_dashboards_page", _page(p)).then(unwrap);

// full detail: rows fields + html, sources:[{source_name, tool, spec}],
// source_conversation, can_edit
export const getDashboard = (name) => call(DB + "get_dashboard", { name }).then(unwrap);

// payload keys: {name?, dashboard_title, description?, html, scope?,
// target_role?, sources?, source_conversation?} - unknown keys throw;
// dashboard_type is derived server-side. -> full detail
export const saveDashboard = (payload = {}) =>
	call(DB + "save_dashboard", { payload: JSON.stringify(payload) }).then(unwrap);

export const deleteDashboard = (name) => call(DB + "delete_dashboard", { name }).then(unwrap);

// The saved dashboard a conversation built, for main chat's "Open in Dashboards"
// affordance. -> {name, dashboard_title} | {} (an unsaved build is the normal
// empty answer, not an error). Throws if the caller does not own the chat.
export const dashboardForConversation = (conversation) =>
	call(DB + "dashboard_for_conversation", { conversation }).then(unwrap);

// View-mode data: executes the SERVER-stored spec for one named source.
// -> {ok:true, data:{source_name, tool, rows, columns? (run_report only),
//     truncated, took_ms}}
//  | {ok:false, error:{code:"PermissionError"|"InvalidArgumentError"|"NotFound"
//     |"InternalError", message}}   (envelope passed through, NOT unwrapped)
export const runDashboardSource = (dashboard, source_name) =>
	call(DB + "run_dashboard_source", { dashboard, source_name });

// Builder-mode ad-hoc data: the iframe's declared spec executed directly via
// the generic tool endpoint. Client-side whitelist mirrors what dashboards may
// declare; anything else short-circuits to the same error envelope shape.
// -> {ok, data} | {ok:false, error:{code, message}}  (NOT unwrapped)
const DASH_TOOLS = ["query", "get_list", "run_report"];
export const callDashboardTool = (tool, args = {}) => {
	if (!DASH_TOOLS.includes(tool)) {
		return Promise.resolve({
			ok: false,
			error: {
				code: "InvalidArgumentError",
				message: `Tool "${tool}" is not allowed in dashboards.`,
			},
		});
	}
	return call("jarvis.api.call_tool", { tool, args: JSON.stringify(args || {}) });
};

// ── chat pane (existing chat endpoints, reused - the triggers.js shape) ───────
// api.js#sendMessage only forwards `context` when it carries a doctype, so the
// dashboards pane binds send_message directly with its page context. Empty
// conversation is allowed - the backend creates/focuses one and returns its id
// as `conversation_id`. -> {ok, run_id, message_id, conversation_id} or
// {ok: false, reason} (NOT the {ok, data} envelope - passed through as-is).
// dataMode: "static" | "live" | anything else = Auto (backend forwards only the
// two literal values). editingName: when revising a saved dashboard, its name —
// forwarded as {doctype, name} so the agent gets a [Viewing:] line and reads the
// current html/sources before changing them. theme: the selected theme key
// (lowercase) — forwarded so the backend tells the agent which theme to design
// for and the dashboards skill injects that theme's token+recipe cheatsheet.
export const sendDashboardChat = (
	conversation,
	message,
	dataMode = "",
	editingName = "",
	theme = ""
) => {
	const context = { page: "dashboards" };
	if (dataMode === "static" || dataMode === "live") context.data_mode = dataMode;
	if (theme) context.theme = theme;
	if (editingName) {
		context.doctype = "Jarvis Dashboard";
		context.name = editingName;
	}
	return call("jarvis.chat.api.send_message", {
		conversation: conversation || "",
		message,
		context: JSON.stringify(context),
		background: 0,
	});
};

// -> {conversation: {name, title, ...}, messages: [{name, seq, role, content,
//     streaming, error, ...}]} - raises DoesNotExistError for a deleted chat.
export const getDashboardConversation = (conversation) =>
	call("jarvis.chat.api.get_conversation", { conversation });
