// Triggers-page API wrappers - thin bindings around `jarvis.chat.triggers_api.*`
// (src/api.js is frozen - new endpoints get per-feature modules under src/api/,
// the api/macros.js convention). Every triggers_api endpoint answers with the
// {ok: true, data: ...} envelope, so each wrapper unwraps `.data` here and hands
// the page components plain payloads (the paginated ones return the frozen list
// envelope {rows, total, has_more, start, page_length}).
import { call } from "frappe-ui";
import { listPageArgs as _page } from "./listPageArgs";

const TR = "jarvis.chat.triggers_api.";

// {ok, data} → data (defensive: a bare payload passes through untouched)
function unwrap(res) {
	return res && typeof res === "object" && res.data !== undefined ? res.data : res;
}

// Request args come from the ONE shared encoder (plan 08 P0-01). This wrapper's
// private `_page` used to rebuild the request WITHOUT `filters_v2`, so a Triggers
// clause silently never reached the endpoint — the exact defect the round-2
// review found. It now routes through `listPageArgs` like every other migrated
// wrapper, so the drift cannot return.

// ── caps probe ────────────────────────────────────────────────────────────────
// -> {can_manage, scripts_enabled, stt_enabled, events: [{value,label}],
//     llm_events: [values]}
export const getTriggersCaps = () => call(TR + "get_triggers_caps").then(unwrap);

// ── triggers ──────────────────────────────────────────────────────────────────
// rows: {name, trigger_name, enabled, target_doctype, doc_event, action_type,
//        description, modified, owner, last_activity_at, activity_24h}
export const listTriggersPage = (p) => call(TR + "list_triggers_page", _page(p)).then(unwrap);

// full detail: rows fields + condition, script_body, llm_instruction,
// llm_daily_cap, server_script, source_conversation
export const getTrigger = (name) => call(TR + "get_trigger", { name }).then(unwrap);

// payload fields: trigger_name, enabled, target_doctype, doc_event, condition,
// action_type ('Script'|'LLM'), script_body, llm_instruction, llm_daily_cap,
// description, source_conversation - JSON-encoded (dict arg). -> full detail
export const createTrigger = (payload = {}) =>
	call(TR + "create_trigger", { payload: JSON.stringify(payload) }).then(unwrap);
export const updateTrigger = (name, payload = {}) =>
	call(TR + "update_trigger", { name, payload: JSON.stringify(payload) }).then(unwrap);

export const setTriggerEnabled = (name, enabled) =>
	call(TR + "set_trigger_enabled", { name, enabled: enabled ? 1 : 0 }).then(unwrap);

export const deleteTrigger = (name) => call(TR + "delete_trigger", { name }).then(unwrap);
// cap 50 server-side - callers pre-check the count for a clean error
export const deleteTriggersBulk = (names) =>
	call(TR + "delete_triggers_bulk", {
		names: JSON.stringify(Array.from(names || [])),
	}).then(unwrap);

// -> {valid: bool, error?: string, would_fire?: bool}
export const testTriggerCondition = (target_doctype, condition, docname = "") =>
	call(TR + "test_trigger_condition", { target_doctype, condition, docname }).then(unwrap);

// ── activity ──────────────────────────────────────────────────────────────────
// rows: {name, trigger, trigger_label, target_doctype, target_docname,
//        doc_event, action_type, status, summary, detail, duration_ms,
//        event_user, creation}; filters: trigger, status, action_type,
//        target_doctype, doc_event, from_date, to_date. Non-admin `total` is
//        approximate (the ListFooter count is best-effort for them).
export const listActivityPage = (p) => call(TR + "list_activity_page", _page(p)).then(unwrap);

// -> {by_status: {...}, total} for admins, {} for everyone else
export const activityStats = () => call(TR + "activity_stats").then(unwrap);

// ── chat pane (existing chat endpoints, reused) ───────────────────────────────
// api.js#sendMessage only forwards `context` when it carries a doctype, so the
// triggers pane binds send_message directly with its page context. Empty
// conversation is allowed - the backend creates/focuses one and returns its id
// as `conversation_id`. -> {ok, run_id, message_id, conversation_id} or
// {ok: false, reason} (NOT the {ok, data} envelope - passed through as-is).
export const sendTriggerChat = (conversation, message) =>
	call("jarvis.chat.api.send_message", {
		conversation: conversation || "",
		message,
		context: JSON.stringify({ page: "triggers" }),
		background: 0,
	});

// -> {conversation: {name, title, ...}, messages: [{name, seq, role, content,
//     streaming, error, ...}]} - raises DoesNotExistError for a deleted chat.
export const getTriggerConversation = (conversation) =>
	call("jarvis.chat.api.get_conversation", { conversation });
