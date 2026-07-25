// Thin wrappers around frappe-ui's `call` (which posts to /api/method/... with
// the session cookie + CSRF). Same backend the Desk chat uses, so conversations
// stay consistent across surfaces.
import { call } from "frappe-ui";

export const listConversations = () => call("jarvis.chat.api.list_conversations");
export const listTools = () => call("jarvis.chat.api.list_tools");
export const getConversation = (conversation) =>
	call("jarvis.chat.api.get_conversation", { conversation });
// Rich outputs: fetch one canvas/chart artifact's render-ready HTML for an
// assistant message (sandboxed-iframe srcdoc). `name` selects which artifact
// when a message has several.
export const getCanvas = (message, name, dark) =>
	call("jarvis.chat.api.get_canvas", { message, name: name || "", dark: dark ? 1 : 0 });
// Tabular/text preview for the artifact side panel (xlsx/csv → sheets, txt → text).
export const previewFile = (fileUrl) =>
	call("jarvis.chat.api.preview_file", { file_url: fileUrl });
export const createOrFocusEmpty = () => call("jarvis.chat.api.create_or_focus_empty");
export const archiveConversation = (conversation) =>
	call("jarvis.chat.api.archive_conversation", { conversation });
// Danger zone: permanently delete ALL of the user's conversations + messages.
export const clearChatHistory = () => call("jarvis.chat.api.clear_chat_history");
export const renameConversation = (conversation, title) =>
	call("jarvis.chat.api.rename_conversation", { conversation, title });
export const setStar = (conversation, starred) =>
	call("jarvis.chat.api.set_star", { conversation, starred: starred ? 1 : 0 });
export const retryMessage = (message) => call("jarvis.chat.api.retry_message", { message });
export const getChatUiSettings = () => call("jarvis.chat.api.get_chat_ui_settings");
// Toggle per-conversation "auto-apply changes" (skip the write-safety
// confirmation before mutating ERP data). Off = confirm every gated write
// (default). Enabling requires System Manager (a non-admin gets a 403);
// disabling is always allowed for the owner. Response: {ok, data:{auto_apply}}.
export const setAutoApply = (conversation, value) =>
	call("jarvis.chat.api.set_auto_apply", { conversation, value: value ? 1 : 0 });
// Estimated token usage (this chat / this month / total + monthly budget).
// Response also carries a "measured" block (real gateway-recorded counters +
// the caller's own monthly_token_limit) once the backend records usage —
// null/zeros until then (design doc §6, UsagePane's "Measured usage" block).
export const getUsage = (conversation) =>
	call("jarvis.chat.api.get_usage", { conversation: conversation || "" });
export const isReadyForChat = () => call("jarvis.account.is_ready_for_chat");

// --- Per-user chat settings + real (measured) usage tracking, incl. the
// tenant-admin usage table (design doc §4/§6). All return the house
// {ok, data} / {ok:false, reason} envelope — unlike getUsage above, which is
// a flat legacy dict. ---
const US = "jarvis.chat.user_settings_api.";
export const getMySettings = () => call(US + "get_my_settings");
export const updateMySettings = (p) => call(US + "update_my_settings", p || {});
export const setUserTheme = (theme) => call(US + "set_user_theme", { theme });
// Jarvis Admin (or System Manager) only — server re-checks independently of
// the client's window.is_jarvis_admin gate.
export const adminListUserUsage = () => call(US + "admin_list_user_usage");
export const adminSetUserLimit = (user, monthlyTokenLimit) =>
	call(US + "admin_set_user_limit", { user, monthly_token_limit: monthlyTokenLimit });
export const adminSetUserModelLimit = (user, model, monthlyTokenLimit) =>
	call(US + "admin_set_user_model_limit", {
		user,
		model,
		monthly_token_limit: monthlyTokenLimit,
	});
export const adminSyncUsage = () => call(US + "admin_sync_usage");

// --- Whitelabel branding (tenant-admin only; server re-checks) ---
const BR = "jarvis.branding.";
export const getBranding = () => call(BR + "get_branding");
export const updateBranding = (p) => call(BR + "update_branding", p || {});

// --- Mobile app onboarding: QR the phone scans to learn the site connection
// details (no secret — just where to reach this site). ---
export const getPairingQr = () => call("jarvis.mobile.auth.get_pairing_qr");

// --- Custom skills (customer-authored, pushed to the container) ---
const SK = "jarvis.chat.custom_skills_api.";
export const listCustomSkills = () => call(SK + "list_custom_skills");
export const getCustomSkill = (name) => call(SK + "get_custom_skill", { name });
export const createCustomSkill = (p) => call(SK + "create_custom_skill", p);
export const updateCustomSkill = (p) => call(SK + "update_custom_skill", p);
export const deleteCustomSkill = (name) => call(SK + "delete_custom_skill", { name });
export const applyCustomSkills = () => call(SK + "apply_custom_skills");
export const getCustomSkillsSyncStatus = () => call(SK + "get_custom_skills_sync_status");
// Skill sharing: owner shares a skill with specific users (read-only for them).
export const listShareableUsers = () => call(SK + "list_shareable_users");
export const getSkillShares = (name) => call(SK + "get_skill_shares", { name });
export const shareCustomSkill = (name, users) =>
	call(SK + "share_custom_skill", { name, users: JSON.stringify(users || []) });

// --- Macros (customer-authored prompt sequences run as chained turns) ---
const MC = "jarvis.chat.macros_api.";
export const listMacros = () => call(MC + "list_macros");
export const getMacro = (name) => call(MC + "get_macro", { name });
export const createMacro = (p) =>
	call(MC + "create_macro", { ...p, steps: JSON.stringify(p.steps || []) });
export const updateMacro = (p) => {
	const args = { ...p };
	if (p.steps !== undefined) args.steps = JSON.stringify(p.steps);
	return call(MC + "update_macro", args);
};
export const deleteMacro = (name) => call(MC + "delete_macro", { name });
export const runMacro = (name) => call(MC + "run_macro", { name });
export const stopMacroRun = (run) => call(MC + "stop_macro_run", { run });
// Run-history dashboard (settings → Macro runs).
export const listMacroRuns = (params) => call(MC + "list_macro_runs", params || {});
export const macroRunStats = () => call(MC + "macro_run_stats");
// Background summarize: fired after every 2+ step save; the WORKER applies the
// summary when the turn ends (macro:merged event) - no client round-trip needed.
// Run is gated on merge_status while pending.
export const summarizeMacro = (name) => call(MC + "summarize_macro", { name });
export const setConversationModel = (conversation, model) =>
	call("jarvis.chat.api.set_conversation_model", { conversation, model: model || "" });
// Reasoning effort. "" clears the override, so the turn inherits Jarvis Settings.
// The server maps this onto openclaw's "/think <level>" directive.
export const setConversationThinking = (conversation, thinking) =>
	call("jarvis.chat.api.set_conversation_thinking", { conversation, thinking: thinking || "" });

// --- Recurring business-note greeting banner ---
// maybe_greet takes no user argument on purpose: it always acts on the session
// user, so one user can never trigger a greeting for another. All gating
// (every-third-chat cadence / dismissed / has-notes) lives on the server.
const GR = "jarvis.chat.greeting.";
export const maybeGreet = () => call(GR + "maybe_greet");
// "Maybe later" / the card's X: hides for the rest of the current cadence tick
// (survives refresh); the card returns on the next multiple-of-three chat.
export const hideGreeting = () => call(GR + "hide_greeting");
export const dismissGreeting = () => call(GR + "dismiss_greeting");

// --- Record draft panel (direct apply, no LLM in the loop) ---
const AC = "jarvis.chat.actions_api.";
export const getDoctypeFormMeta = (doctype) => call(AC + "get_doctype_form_meta", { doctype });
export const loadDocForEdit = (doctype, name) => call(AC + "load_doc", { doctype, name });
export const applyAction = (action) =>
	call(AC + "apply_action", { action: JSON.stringify(action) });
// Write-safety gate (issue #186): confirm a parked ERP write by its one-time
// token. Pass the conversation the click came from so the server enforces the
// real conversation guard (#11). Returns the tool result envelope {ok, ...} on
// success, or {ok:False, error:{type:"InvalidConfirmation", ...}} if the token
// is gone.
export const confirmTool = (token, conversation) =>
	call(AC + "confirm_tool", { token, conversation: conversation || "" });
// Discard a parked ERP write by its one-time token: consumes the token (so it
// can't replay or re-surface on reload), leaves a durable "discarded" receipt
// chip in the transcript, and queues a note so the agent's next turn learns it
// was vetoed. Returns {ok, data:{status:"discarded"|"already_handled"}}; the SPA
// drops the card either way.
export const dismissTool = (token, conversation) =>
	call(AC + "dismiss_tool", { token, conversation: conversation || "" });
// Resync (issue #186, R3 fix for #3): re-surface the caller's own currently
// parked confirmation cards after a reload/reconnect. Returns
// {ok, data:{pending:[{token, tool, preview, summary, conversation, run_id}]}}.
export const listPendingConfirmations = (conversation) =>
	call(AC + "list_pending_confirmations", { conversation: conversation || "" });

export async function sendMessage(conversation, message, modelOverride, attachments, context) {
	// Empty conversation is allowed: the backend creates (or focuses) an empty
	// conversation itself and returns its id as `conversation_id` - saves the
	// SPA a createOrFocusEmpty round-trip before the first send (latency plan).
	const args = { conversation: conversation || "", message };
	if (modelOverride) args.model_override = modelOverride;
	if (attachments && attachments.length) args.attachments = JSON.stringify(attachments);
	// Forward context for a viewing-context doc/report OR the one-shot
	// "ground on wiki" flag (which can arrive without a doc).
	if (context && (context.doctype || context.ground_wiki))
		args.context = JSON.stringify(context);
	return call("jarvis.chat.api.send_message", args);
}

// Actually abort a running turn (openclaw chat.abort) — best-effort; on failure
// the UI stop stands and the turn finishes server-side.
export async function stopRun(conversation, runId) {
	return call("jarvis.chat.api.stop_run", { conversation, run_id: runId || "" });
}

// Phase-0 admission (chat concurrency): cancel a turn that is still QUEUED
// (behind other turns, not yet dispatched). Owner-checked server-side.
export const cancelQueuedTurn = (runId) =>
	call("jarvis.chat.admission.cancel_queued_turn", { run_id: runId });

// Poll-on-focus fallback for the queued chip: returns {state, position}.
export const getQueuePosition = (runId) =>
	call("jarvis.chat.admission.queue_position", { run_id: runId });

// Server-truth resync for the queued chip (SUXI-1): the conversation's own
// non-terminal turn, if any. Rebuilds the chip after a reload / conversation
// switch / second tab / WS reconnect (the queued chip is otherwise client-only).
export const getActiveTurn = (conversation) =>
	call("jarvis.chat.admission.active_turn_for_conversation", { conversation });

// Mentions: reuse Frappe's built-in Link-field search (no custom backend).
export const searchLink = (doctype, txt) =>
	call("frappe.desk.search.search_link", { doctype, txt: txt || "", page_length: 8 });

// Field metadata for the record-edit action card: powers Link/Select/Date
// controls (returns {ok, doctype, fields:[{fieldname,label,fieldtype,options}]}).
export const getDoctypeFields = (doctype) =>
	call("jarvis.chat.api.get_doctype_fields", { doctype });

// --- LLM pool / models config (System-Manager only, gated server-side) ---
export const getLlmConfig = () => call("jarvis.onboarding.get_llm_config");
export const getPresetCatalog = () => call("jarvis.onboarding.get_preset_catalog");
// Admin-managed model catalog slice (api_key_models, subscription_models,
// default_models) for the pool editor + subscription card. Independent of
// get_chat_ui_settings so it also works in the onboarding wizard, which never
// calls that. See jarvis.chat.api.get_model_catalog_ui.
export const getModelCatalogUi = () => call("jarvis.chat.api.get_model_catalog_ui");
export const getLlmSyncStatus = () => call("jarvis.onboarding.get_llm_sync_status");
export const saveLlmPool = (models, preset = null, routingMode = "failover") =>
	call("jarvis.onboarding.save_llm_pool", {
		models: JSON.stringify(models),
		preset: preset || "",
		routing_mode: routingMode,
	});
// Pre-save "Test" probe for ONE api-key model row (never persists, never
// touches the fleet/container - see jarvis/llm_key_probe.py). args:
// {provider, model, api_key, base_url}. Returns
// {ok, checks:[{check,ok,detail}], provider, local_endpoint, caveat}.
export const testLlmApiKey = (args) => call("jarvis.llm_key_probe.test_llm_api_key", args);

// --- Onboarding wizard (managed signup + self-hosted connect) ---
// Arg names mirror the real backend signatures (jarvis/onboarding.py,
// jarvis/account.py, jarvis/selfhost.py) - verified against the desk wizard's
// frappe.call usage in jarvis/jarvis/page/jarvis_onboarding/jarvis_onboarding.js.
export const listPlans = () => call("jarvis.onboarding.list_plans");
export const getAccountDefaults = () => call("jarvis.onboarding.get_account_defaults");
export const syncConnection = () => call("jarvis.onboarding.sync_connection");
export const startSignup = (email, company, plan) =>
	call("jarvis.onboarding.start_signup", { email, company, plan });
export const checkSignupPaymentState = () => call("jarvis.onboarding.check_signup_payment_state");
export const finishPayment = (payload) => call("jarvis.onboarding.finish_payment", { payload });
export const isOnboarded = () => call("jarvis.account.is_onboarded");
// args: {provider, model, api_key, base_url, auth_mode, force}
export const saveLlmCreds = (args) => call("jarvis.onboarding.save_llm_creds", args);
// args: {base_url, token, deep, stream, tool_user}
export const saveSelfHosted = (args) => call("jarvis.selfhost.save_self_hosted", args);
// args: {base_url, token, deep}
export const testSelfHostConnection = (args) => call("jarvis.selfhost.test_connection", args);

// --- Per-account chat-subscription capture (paste-back OAuth) ---
// begin → { nonce, authorize_url, expires_in }; open authorize_url, sign in,
// paste the redirected URL back → complete captures the account (no side effects).
export const beginPoolAccountSignin = (provider, model) =>
	call("jarvis.oauth.api.begin_pool_account_signin", { provider, model });
// complete is capture-only → { account_ref, label, oauth_blob, account_email }
export const completePoolAccountSignin = (nonce, redirectedUrl) =>
	call("jarvis.oauth.api.complete_pool_account_signin", {
		nonce,
		redirected_url: redirectedUrl,
	});
// Device-code (Kimi) capture: begin returns { device_flow:true, user_code,
// verification_uri, interval }; poll on `interval` → { status:"pending" } until
// the user approves, then the same capture-only { account_ref, label, oauth_blob }.
export const pollPoolAccountSignin = (nonce) =>
	call("jarvis.oauth.api.poll_pool_account_signin", { nonce });

// --- DIRECT single chat-subscription (legacy flat-field path) ---------------
// Existing customers who onboarded a single chat subscription keep their creds
// in the flat llm_*/llm_oauth_* fields (NOT the models[] pool). get_llm_config
// can't see them, so AccountView probes this to decide whether to render the
// DIRECT re-authorize card instead of the pool editor. Returns
// { is_direct_subscription, connected, auth_mode, provider, model,
//   account_email, connected_at }.
export const getDirectSubscriptionStatus = () =>
	call("jarvis.oauth.api.get_direct_subscription_status");
// DIRECT paste-back re-authorize: begin → { ok, data:{nonce, authorize_url,
// expires_in} }; complete pushes the fresh blob to the container's
// auth-profiles.json + rewrites the flat fields (force restart). These write
// Jarvis Settings - unlike the pool "capture-only" variants above.
export const beginPasteSignin = (provider, model) =>
	call("jarvis.oauth.api.begin_paste_signin", { provider, model });
export const completePasteSignin = (nonce, redirectedUrl) =>
	call("jarvis.oauth.api.complete_paste_signin", { nonce, redirected_url: redirectedUrl });
// Tear down the direct subscription (clears the container profile + flat fields,
// flips bench back to api_key). Returns { ok } / { ok:false, error }.
export const disconnectSubscription = () => call("jarvis.oauth.api.disconnect");

// --- LLM Monitor (System-Manager gated server-side). Real Bifrost usage, NOT the getUsage estimate. ---
export const getLlmUsage = () => call("jarvis.account.get_llm_usage");
export const getLlmConnectionStatus = () => call("jarvis.account.get_llm_connection_status");
export const getAccount = () => call("jarvis.account.get_account");
// BILLING plan lifecycle. Not to be confused with disconnectSubscription
// above, which drops the LLM PROVIDER subscription (ChatGPT/Claude OAuth).
// These end / restore the paid Jarvis plan itself.
export const cancelPlanAtPeriodEnd = () => call("jarvis.account.cancel_plan_at_period_end");
export const resumePlan = () => call("jarvis.account.resume_plan");
export const reauthorizeAutopay = () => call("jarvis.account.reauthorize_autopay");
export const previewDowngrade = (targetPlan) =>
	call("jarvis.account.preview_downgrade", { target_plan: targetPlan });
export const startDowngrade = (targetPlan) =>
	call("jarvis.account.start_downgrade", { target_plan: targetPlan });
export const cancelScheduledDowngrade = () => call("jarvis.account.cancel_scheduled_downgrade");

// File input: upload to Frappe's File doctype, return {file_url, file_name}.
export async function uploadFile(file) {
	const fd = new FormData();
	fd.append("file", file, file.name);
	fd.append("is_private", "1");
	const r = await fetch("/api/method/upload_file", {
		method: "POST",
		headers: { "X-Frappe-CSRF-Token": window.csrf_token || "" },
		body: fd,
		credentials: "include",
	});
	if (!r.ok) throw new Error(`upload failed (${r.status})`);
	const data = await r.json();
	const f = data.message || data;
	return { file_url: f.file_url, file_name: f.file_name || file.name };
}

// Branding logo/favicon: PUBLIC file (the favicon <link> and the PWA manifest
// must fetch it without a session cookie), unlike the private uploadFile above.
export async function uploadBrandAsset(file) {
	const fd = new FormData();
	fd.append("file", file, file.name);
	fd.append("is_private", "0");
	const r = await fetch("/api/method/upload_file", {
		method: "POST",
		headers: { "X-Frappe-CSRF-Token": window.csrf_token || "" },
		body: fd,
		credentials: "include",
	});
	if (!r.ok) throw new Error(`upload failed (${r.status})`);
	const data = await r.json();
	const f = data.message || data;
	return { file_url: f.file_url, file_name: f.file_name || file.name };
}

// ── File Box: drop an inbound document, get a directed processing chat ──
export const fileboxDrop = (file_url, file_name) =>
	call("jarvis.chat.filebox.drop_file", { file_url, file_name });
export const fileboxList = () => call("jarvis.chat.filebox.list_inbound", {});

// ── Approvals: pending-decision queue + decide-and-resume ──
export const listApprovals = (status = "Pending") =>
	call("jarvis.chat.approvals_api.list_approvals", { status });
export const approvalsPendingCount = () => call("jarvis.chat.approvals_api.pending_count", {});
export const decideApproval = (name, decision, approve = 1) =>
	call("jarvis.chat.approvals_api.decide", { name, decision, approve });
// Ignore a request off the board (no verdict, no chat resume); reversible.
export const dismissApproval = (name) =>
	call("jarvis.chat.approvals_api.dismiss_approval", { name });
export const restoreApproval = (name) =>
	call("jarvis.chat.approvals_api.restore_approval", { name });

// ── Agents Marketplace: catalog, install/enable/schedule, apply, runs+findings ──
// enable/disable + schedule + config are INSTANT (pure DB writes). Apply is the
// only call that reconciles the container (install/uninstall/update → restart),
// polled via getAgentsSyncStatus exactly like the custom-skills apply pill.
const AG = "jarvis.chat.agents_api.";
export const listAgents = () => call(AG + "list_agents");
export const getAgentInstallations = () => call(AG + "get_installations");
export const installAgent = (agent_slug) => call(AG + "install_agent", { agent_slug });
export const uninstallAgent = (installation) => call(AG + "uninstall_agent", { installation });
export const setAgentEnabled = (installation, enabled) =>
	call(AG + "set_enabled", { installation, enabled: enabled ? 1 : 0 });
export const setAgentSchedule = (installation, p) =>
	call(AG + "set_schedule", { installation, ...(p || {}) });
// Engagement config JSON (benchmark_value, percentage, engagement_risk_level,
// rounding_step) read by the agent on its installation.
export const setAgentConfig = (installation, config) =>
	call(AG + "set_config", { installation, config: JSON.stringify(config || {}) });
export const runAgentNow = (installation) => call(AG + "run_agent_now", { installation });
export const applyAgents = () => call(AG + "apply_agents");
export const getAgentsSyncStatus = () => call(AG + "get_agents_sync_status");
export const listAgentRuns = (agent, limit) =>
	call(AG + "list_runs", { agent: agent || "", limit: limit || 50 });
export const listAgentFindings = (p) => call(AG + "list_findings", p || {});
export const setFindingState = (finding, state) =>
	call(AG + "set_finding_state", { finding, state });
// Role gating + listing admin (System Manager only - the server enforces it;
// the SPA merely probes getAgentAdminOverview and hides the Admin tab on 403).
export const setAgentRoles = (agent_slug, roles) =>
	call(AG + "set_agent_roles", { agent_slug, roles: JSON.stringify(roles || []) });
export const setListingStatus = (agent_slug, status) =>
	call(AG + "set_listing_status", { agent_slug, status });
export const getAgentAdminOverview = () => call(AG + "get_agent_admin_overview");

// ── Paginated feature-page lists (design §2.7) ──────────────────────────────
// One frozen envelope for all four features:
//   { rows, total, has_more, start, page_length[, facets] }  (facets: Approvals only;
//   Approvals first-page responses also carry `awaiting_reply` — chat questions
//   with no approval row behind them, rendered by ApprovalsBoard's strip)
// `_page` normalizes the request args (search/filters/sort/paging) exactly as
// the four backend endpoints expect; `filters` is JSON-encoded here so the SPA
// passes a plain object and the server `frappe.parse_json`s it.
const _page = (p = {}) => ({
	search: p.search || "",
	filters: JSON.stringify(p.filters || {}),
	sort_field: p.sort_field || "",
	sort_dir: p.sort_dir || "",
	start: p.start || 0,
	page_length: p.page_length || 20,
});
export const listCustomSkillsPage = (p) => call(SK + "list_custom_skills_page", _page(p));
export const listMacrosPage = (p) => call(MC + "list_macros_page", _page(p));
export const fileboxListPage = (p) => call("jarvis.chat.filebox.list_inbound_page", _page(p));
export const listApprovalsPage = (p) =>
	call("jarvis.chat.approvals_api.list_approvals_page", _page(p));

// ── File Box FB-1: cascade delete + clear-processed + bulk delete (Q4) ──────
// delete_inbound: owner-gated cascade (approvals + messages + File docs + the
// conversation); refuses while the latest assistant message is streaming.
export const fileboxDelete = (conversation) =>
	call("jarvis.chat.filebox.delete_inbound", { conversation });
// clear_processed_inbound: bulk-delete the caller's done|error rows → {ok, deleted}.
export const fileboxClearProcessed = () => call("jarvis.chat.filebox.clear_processed_inbound");
// delete_inbound_bulk (Q4): same cascade + streaming-refusal per row → {deleted, skipped}.
// List JSON-encoded for parity with the other list-argument wrappers.
export const fileboxDeleteBulk = (conversations) =>
	call("jarvis.chat.filebox.delete_inbound_bulk", {
		conversations: JSON.stringify(conversations || []),
	});

// --- Support panel (Plan 3) -------------------------------------------------
export const supportListTickets = () => call("jarvis.support.api.list_tickets");
export const supportCreateTicket = (subject, body) =>
	call("jarvis.support.api.create_ticket", { subject, body: body || "" });
export const supportGetThread = (ticket) => call("jarvis.support.api.get_thread", { ticket });
export const supportReply = (ticket, body) => call("jarvis.support.api.reply", { ticket, body });
export const supportCloseTicket = (ticket) => call("jarvis.support.api.close_ticket", { ticket });
export const supportAwaitingCount = () => call("jarvis.support.api.awaiting_count");

// Same-origin GET so <img>/<a> resolve against the bench; session cookie authenticates.
export const supportDownloadUrl = (ticket, fileUrl) =>
	`/api/method/jarvis.support.media.download?ticket=${encodeURIComponent(
		ticket
	)}&file_url=${encodeURIComponent(fileUrl)}`;

export async function supportUpload(ticket, file) {
	const fd = new FormData();
	fd.append("ticket", ticket);
	fd.append("file", file, file.name);
	const r = await fetch("/api/method/jarvis.support.media.upload", {
		method: "POST",
		headers: { "X-Frappe-CSRF-Token": window.csrf_token || "" },
		body: fd,
		credentials: "include",
	});
	const data = await r.json();
	const msg = data.message || data;
	return msg.data || msg;
}
