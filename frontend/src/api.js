// Thin wrappers around frappe-ui's `call` (which posts to /api/method/... with
// the session cookie + CSRF). Same backend the Desk chat uses, so conversations
// stay consistent across surfaces.
import { call } from "frappe-ui";
import { listPageArgs as _page } from "./api/listPageArgs";

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
// Tool runs recorded in one chat, newest turn first, from the PERSISTED tool
// rows: the same rows the thread's Activity accordion renders. The Settings
// Activity pane used to derive this from the browser's live run stream, which
// read zero on every reload (#551). Response: {runs:[{tools,ms,names}],
// tool_calls}.
export const getToolActivity = (conversation) =>
	call("jarvis.chat.api.get_tool_activity", { conversation });
// ---- account reconnect (fresh bench -> existing paid subscription) ---------
export const reconnectAvailable = (email, company = "") =>
	call("jarvis.onboarding.reconnect_available", { email, company });
export const startAccountReconnect = (email, company = "") =>
	call("jarvis.onboarding.start_account_reconnect", { email, company });
export const checkAccountReconnect = (requestId, code) =>
	call("jarvis.onboarding.check_account_reconnect", { request_id: requestId, code: code || "" });

export const isReadyForChat = () => call("jarvis.account.is_ready_for_chat");
// Read-only poll of a durable LLM-apply operation (plan-05 D2). The single
// Start-chatting controller (frontend/src/lib/llmOperation.js) follows exactly one
// operation id to a terminal state through this; it never spends the apply bucket.
export const getLlmApplyOperation = (operationId) =>
	call("jarvis.account.get_llm_apply_operation", { operation_id: operationId });

// ---- workspace reset (customer self-serve, admin-gated) --------------------
export const requestWorkspaceReset = (reason, opts = {}) =>
	call("jarvis.onboarding.request_workspace_reset", {
		reason: reason || "",
		wipe_data: opts.wipeData ? 1 : 0,
		revoke_llm: opts.revokeLlm ? 1 : 0,
	});
export const workspaceResetState = () => call("jarvis.onboarding.workspace_reset_state");

// --- Per-user chat settings + real (measured) usage tracking, incl. the
// tenant-admin usage table (design doc §4/§6). All return the house
// {ok, data} / {ok:false, reason} envelope — unlike getUsage above, which is
// a flat legacy dict. ---
const US = "jarvis.chat.user_settings_api.";
export const getMySettings = () => call(US + "get_my_settings");
export const updateMySettings = (p) => call(US + "update_my_settings", p || {});
export const setUserTheme = (theme) => call(US + "set_user_theme", { theme });
// Best-effort ack that the chat-home introduction (the static welcome bubble)
// was shown. Idempotent + monotonic server-side; the caller swallows failures,
// whose only cost is the introduction appearing once more.
export const markHomeIntroSeen = (version) =>
	call(US + "mark_home_intro_seen", { version: version || 0 });
// Bounded, privacy-free chat-home introduction telemetry (displayed /
// suggestion_selected / first_prompt). The server allow-lists every field and
// hashes the caller — no message content, prompt text, or user name is sent.
// Best-effort: the caller swallows failures, telemetry must never affect chat.
export const recordHomeIntroEvent = (event, payload) =>
	call(US + "record_home_intro_event", {
		event,
		version: (payload && payload.version) || 0,
		category: (payload && payload.category) || "",
		bucket: (payload && payload.bucket) || "",
	});
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
// `pageLength` defaults to the mention picker's 8; the list FilterGroup asks for
// plan 08 §9's Link-suggestion cap of 20. Frappe's own search_link enforces the
// target DocType's read permission + user permissions, which is why plan 08 adds
// no second search endpoint.
//
// `referenceDoctype` / `linkFieldname` (plan 08 P1-06) name WHERE the Link is
// used, so a DocType's custom `search_link` hook and user-permission behaviour
// can key off the field. They are untrusted CONTEXT — the server still enforces
// the target's read permission regardless — and are sent only when known
// (mentions omit them). The list FilterGroup passes the schema entry's own
// (doctype, fieldname), which the shared schema already knows.
export const searchLink = (doctype, txt, pageLength, referenceDoctype, linkFieldname) => {
	const args = { doctype, txt: txt || "", page_length: pageLength || 8 };
	// Frappe's search_link params (frappe/desk/search.py): reference_doctype +
	// keyword-only link_fieldname. Sent only when known.
	if (referenceDoctype) args.reference_doctype = referenceDoctype;
	if (linkFieldname) args.link_fieldname = linkFieldname;
	return call("frappe.desk.search.search_link", args);
};

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
// Tear the whole AI connection down: deletes every stored credential on the
// bench (models[] rows + the legacy flat fields) and asks admin to delete them
// from the workspace container. NOT "save an empty pool" - saveLlmPool refuses
// an empty list on purpose. Idempotent. Returns { disconnected: true }.
export const disconnectLlm = () => call("jarvis.onboarding.disconnect_llm");
// Pre-save "Test" probe for ONE api-key model row (never persists, never
// touches the fleet/container - see jarvis/llm_key_probe.py). args:
// {provider, model, api_key, base_url}. Returns
// {ok, checks:[{check,ok,detail}], provider, local_endpoint, caveat}.
export const testLlmApiKey = (args) => call("jarvis.llm_key_probe.test_llm_api_key", args);

// --- Onboarding wizard (managed signup) ---
// Arg names mirror the real backend signatures (jarvis/onboarding.py,
// jarvis/account.py) - verified against the desk wizard's frappe.call usage in
// jarvis/jarvis/page/jarvis_onboarding/jarvis_onboarding.js.
export const listPlans = () => call("jarvis.onboarding.list_plans");
// {providers: [...], default: "..."} - already narrowed to gateways the
// operator enabled AND this bench build can render.
export const listPaymentProviders = () => call("jarvis.onboarding.list_payment_providers");
export const getAccountDefaults = () => call("jarvis.onboarding.get_account_defaults");
export const syncConnection = () => call("jarvis.onboarding.sync_connection");
export const startSignup = (email, company, plan, provider) =>
	call("jarvis.onboarding.start_signup", { email, company, plan, provider });
export const finishPayment = (payload) => call("jarvis.onboarding.finish_payment", { payload });

// --- Payment state machine (plan 02 / plan 03 contract) ---------------------
// These wrap the SAME whitelisted endpoints, but they must NOT go through
// frappe-ui's `call`: `call` throws on a 4xx and keeps only a decoded message,
// while the whole payment contract lives in the RESPONSE BODY - the stamped
// `error` object on a throw, the `{ok, data, context}` envelope on a coded 4xx.
// `rawOnboardingCall` returns `{status, body, networkError}` untouched, and
// @/onboarding/paymentCodec.decode reads it. usePaymentFlow consumes this map.
async function rawOnboardingCall(method, args, opts = {}) {
	const headers = {
		Accept: "application/json",
		"Content-Type": "application/json; charset=utf-8",
		"X-Frappe-Site-Name": window.location.hostname,
	};
	if (window.csrf_token && window.csrf_token !== "{{ csrf_token }}") {
		headers["X-Frappe-CSRF-Token"] = window.csrf_token;
	}
	try {
		const res = await fetch(`/api/method/${method}`, {
			method: "POST",
			headers,
			body: JSON.stringify(args || {}),
			// The client-side deadline (plan 02 P0-3). usePaymentFlow races every
			// payment call against a wall-clock timeout and aborts this signal when it
			// fires, so a stalled proxy/never-settling fetch cannot leave the customer
			// on a permanent busy screen. An abort is a CLIENT signal only - the flow
			// treats it as truth-UNKNOWN and reconciles server truth, never as a
			// payment verdict, because aborting the browser does not cancel server
			// work.
			signal: opts.signal,
		});
		let body = null;
		try {
			body = await res.json();
		} catch (e) {
			body = null; // an HTML error page, an empty 502 - decode() treats it as unreadable
		}
		return { status: res.status, body };
	} catch (e) {
		// fetch itself rejected: offline, DNS, CORS - OR the deadline aborted it. An
		// AbortError must propagate so the flow's timeout race sees the timeout (it
		// is racing this promise against its own timer); everything else is a
		// networkError, never a payment verdict.
		if (e && e.name === "AbortError") throw e;
		return { status: 0, body: null, networkError: true };
	}
}

// The raw-response endpoint map usePaymentFlow is constructed with. Kept as one
// object so the flow's dependency surface is explicit and stubbable in tests.
// Each method takes an optional `{signal}` so the flow can bound it (P0-3).
export const onboardingPaymentApi = {
	getOnboardingState: (opts) =>
		rawOnboardingCall("jarvis.onboarding.get_onboarding_state", {}, opts),
	startSignup: ({ email, company, plan, provider }, opts) =>
		rawOnboardingCall(
			"jarvis.onboarding.start_signup",
			{ email, company, plan, provider },
			opts
		),
	initiateSignupPayment: ({ plan, provider }, opts) =>
		// Deliberately NO idempotency_key: the bench owns that receipt
		// (onboarding_contract.next_idempotency_key). Passing one from the browser
		// invites a replay of a dead intent.
		rawOnboardingCall("jarvis.onboarding.initiate_signup_payment", { plan, provider }, opts),
	checkSignupPaymentStatus: (opts) =>
		rawOnboardingCall("jarvis.onboarding.check_signup_payment_status", {}, opts),
	confirmSignupPayment: (payload, opts) =>
		rawOnboardingCall("jarvis.onboarding.finish_payment", { payload }, opts),
	syncConnection: () => call("jarvis.onboarding.sync_connection"),
};
export const isOnboarded = () => call("jarvis.account.is_onboarded");
// args: {provider, model, api_key, base_url, auth_mode, force}
export const saveLlmCreds = (args) => call("jarvis.onboarding.save_llm_creds", args);

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
// The paying half of the billing page. preview_* prices the change so the
// customer sees the number BEFORE a payment sheet opens; start_* returns the
// gateway handles that sheet needs. Both were already whitelisted for the desk
// page and only lacked an SPA wrapper.
export const previewUpgrade = (targetPlan) =>
	call("jarvis.account.preview_upgrade", { target_plan: targetPlan });
export const startUpgrade = (targetPlan) =>
	call("jarvis.account.start_upgrade", { target_plan: targetPlan });
// Renewal mints its order through the onboarding endpoint, the same one the
// desk page called. Its Checkout result is confirmed by `finishPayment` above
// (declared with the onboarding wrappers), which is the single signature-verify
// endpoint for EVERY billing flow here, not just renewal and first signup.
export const renewPlan = () => call("jarvis.onboarding.renew");

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
// options: per-launch payload. `source_apps` (array of app names) is REQUIRED for
// the custom-app-learning agent - the server refuses the launch without an
// explicit selection and stamps it as the run's source-read authorization.
export const runAgentNow = (installation, options) =>
	call(AG + "run_agent_now", {
		installation,
		...(options ? { options: JSON.stringify(options) } : {}),
	});
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
// Request args (search/filters/sort/paging + filters_v2) come from the ONE
// shared encoder `api/listPageArgs.js` (plan 08 P0-01) — imported as `_page` so
// Skills/Macros/File Box/Approvals and the feature wrappers cannot drift apart.

// plan 08 §6.1: the fields THIS caller may filter `view_key` on, with the
// per-field operators and defaults. Answers only for MIGRATED views; every
// rejection carries a stable `list_filter_*` code (see filterModel.js).
export const getListFilterSchema = (viewKey) =>
	call("jarvis.chat.list_filters.get_list_filter_schema", { view_key: viewKey });
// plan 08 §P1-05 / M1.3: the per-view {view_key: enabled} capability map, so a
// list can learn whether filters_v2 is on for its view BEFORE emitting any clause
// (a rolled-back view must send zero filters_v2 on every path). Cheap — the flag
// map only, no field catalog.
export const getListFilterCapabilities = () =>
	call("jarvis.chat.list_filters.list_filters_capabilities");
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

// Pull the human reason out of a Frappe error response the way frappe-ui's
// `call()` does (it parses `_server_messages` the same way), so errMsg() in
// the support store shows "file too large" / "no file provided" rather than
// a generic status-code string.
function _uploadErrorMessage(data, status) {
	try {
		if (data && data._server_messages) {
			const msgs = JSON.parse(data._server_messages);
			const first = typeof msgs[0] === "string" ? JSON.parse(msgs[0]) : msgs[0];
			if (first && first.message) return String(first.message);
		}
	} catch (e) {
		/* fall through to the status code */
	}
	return `upload failed (${status})`;
}

export async function supportUpload(ticket, file, comm) {
	const fd = new FormData();
	fd.append("ticket", ticket);
	// Attach to the reply Communication (when given) so Helpdesk renders the file inline; a
	// ticket-level File only shows in the sidebar. Appended before the file, so the multipart
	// scalar fields precede the binary part.
	if (comm) fd.append("comm", comm);
	fd.append("file", file, file.name);
	const r = await fetch("/api/method/jarvis.support.media.upload", {
		method: "POST",
		headers: { "X-Frappe-CSRF-Token": window.csrf_token || "" },
		body: fd,
		credentials: "include",
	});
	if (!r.ok) {
		const data = await r.json().catch(() => null);
		throw new Error(_uploadErrorMessage(data, r.status));
	}
	const data = await r.json();
	const msg = data.message || data;
	return msg.data || msg;
}
