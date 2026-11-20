// Thin wrappers around frappe-ui's `call` (POSTs /api/method/... with the
// session cookie + CSRF header). The PWA is served BY the site, so it is the
// same auth the Desk and the desktop SPA use — no pairing token, no QR: that
// flow only exists in the native app because it lives on another origin.
//
// Deliberately its own module rather than an import of the SPA's api.js: this
// surface needs ~20 of that file's 60-odd endpoints, and keeping it separate
// means the phone bundle can't be grown by a change made for the desktop.
// Voice is the exception — see transcribeAudio below.
import { call } from "frappe-ui";
import { serverReason } from "../../jarvis/public/js/shared/chat_failure_copy.mjs";
import { HISTORY_PAGE_SIZE } from "@shared/lib/historyPaging.js";

const CHAT = "jarvis.chat.api.";

export const listConversations = () => call(CHAT + "list_conversations");
// The newest page of the thread, or with {before} the page older than that cursor;
// {refresh} names held older rows to send fresh and {after} is the newest seq held
// (#715 F13, @shared/lib/historyPaging.js).
export const getConversation = (conversation, { before, refresh, after } = {}) =>
	call(CHAT + "get_conversation", {
		conversation,
		limit: HISTORY_PAGE_SIZE,
		...(before ? { before } : {}),
		...(after ? { after } : {}),
		...(refresh && refresh.length ? { refresh: JSON.stringify(refresh) } : {}),
	});
export const archiveConversation = (conversation) =>
	call(CHAT + "archive_conversation", { conversation });
export const renameConversation = (conversation, title) =>
	call(CHAT + "rename_conversation", { conversation, title });
export const setStar = (conversation, starred) =>
	call(CHAT + "set_star", { conversation, starred: starred ? 1 : 0 });

// Model name for the chat header, the model/effort pickers, and whether the mic
// is allowed to appear (STT is off unless the admin configured a transcription
// key).
export const getChatUiSettings = () => call(CHAT + "get_chat_ui_settings");

// Starter prompts for the empty new-chat screen: a few example asks, synthesised
// server-side from the user's own recent chats (the same endpoint the desktop
// SPA uses). Lives in user_settings_api, not the CHAT namespace.
export const getPromptSuggestions = () =>
	call("jarvis.chat.user_settings_api.get_prompt_suggestions");

// The holiday welcome scene asks the server for its showing slot (jarvis.holiday_touch.api).
// Answers {play: bool}; rejects on a network or server error. frappe-ui's `call` POSTs with
// the CSRF header, which this method needs. The scene gives it, and the art import, one
// overall deadline (lib/useHolidayScene.js), so this call sets no timeout of its own.
export const recordHolidayShown = () => call("jarvis.holiday_touch.api.record_shown");

// The caller's own prefs. Carries default_auto_mode and auto_mode_acknowledged
// (#581); update_my_settings takes either. Returns {ok, data}.
const US = "jarvis.chat.user_settings_api.";
export const getMySettings = () => call(US + "get_my_settings");
export const updateMySettings = (p) => call(US + "update_my_settings", p || {});

// An empty `conversation` is allowed: the backend creates (or focuses) the
// user's empty conversation and returns its id as `conversation_id`, which
// saves the new-chat round-trip before the very first send.
//
// model/thinking are sent as per-turn overrides, which is how the new-chat
// screen applies the device's preferred model without mutating the workspace
// default.
export const sendMessage = (
	conversation,
	message,
	{ attachments = [], model, thinking, approvalTokens, autoMode } = {}
) =>
	call(CHAT + "send_message", {
		conversation: conversation || "",
		message,
		...(attachments.length ? { attachments: JSON.stringify(attachments) } : {}),
		...(model ? { model_override: model } : {}),
		...(thinking ? { thinking_override: thinking } : {}),
		// Ordered tokens of the on-screen confirmation cards, so a typed "confirm 2"
		// binds to the card shown at that number rather than a server-re-fetched
		// position that a mid-flight expiry could renumber onto the wrong write.
		...(approvalTokens && approvalTokens.length
			? { approval_tokens: JSON.stringify(approvalTokens) }
			: {}),
		// Auto mode (#581) is chosen with a chat's first message only; the server
		// ignores it on any chat that already has messages.
		...(autoMode ? { auto_mode: 1 } : {}),
	});

export const stopRun = (conversation, runId) =>
	call(CHAT + "stop_run", { conversation, run_id: runId || "" });

// A message's rich outputs (charts, generated images, files). `get_conversation`
// returns the item list on `message.canvas`; this fetches one item's body.
export const getCanvas = (message, name = "", dark = 0) =>
	call(CHAT + "get_canvas", { message, name, dark });

// Render-ready preview of a tabular/text artifact (xlsx/csv → sheets, txt → text).
export const previewFile = (file_url) => call(CHAT + "preview_file", { file_url });

// Predefined PDF templates: list the set + reformat a generated PDF into another
// one (the per-document override). Mirrors the desktop SPA; the phone has no admin
// pane, so the workspace-default get/set endpoints are intentionally not wired here.
const PT = "jarvis.pdf_templates.";
export const listPdfTemplates = () => call(PT + "list_pdf_templates");
export const rerenderDocument = (source_name, template) =>
	call(PT + "rerender_document", { source_name, template });

// ── Account: who you are, what you're on, what you've used ──────────────────
export const getAccount = () => call("jarvis.account.get_account");
export const getUsage = () => call(CHAT + "get_usage");

// ── Business (Personalise): what the agent knows about how you work ─────────
// Notes are captured typed or dictated and processed into learned defaults and
// the org wiki. Same endpoints as the web's Personalise tab.
const VN = "jarvis.chat.voice_notes_api.";
export const getBusinessStatus = () => call(VN + "get_business_status");
export const listVoiceNotes = (start = 0, page_length = 20, search = "") =>
	call(VN + "list_my_voice_notes_page", { start, page_length, ...(search ? { search } : {}) });
// `source` is deliberately NOT sent: it is a server allowlist
// (voice_notes_api._SOURCES) and anything off it is rejected with "Invalid
// source". Letting the server apply its own default keeps this call correct
// even as that list changes. (The native app does send source: "Mobile", which
// the allowlist now carries — see the contract test in
// jarvis/tests/test_voice_notes_crud.py.)
export const saveVoiceNote = (transcript, duration_s = 0) =>
	call(VN + "save_voice_note", { transcript, context_type: "Business", duration_s });
export const deleteVoiceNote = (name) => call(VN + "delete_voice_note", { name });

// ── File Box: drop a document, get a chat that has already read it ──────────
export const listInbound = (start = 0, page_length = 20, search = "") =>
	call("jarvis.chat.filebox.list_inbound_page", {
		search,
		filters: "{}",
		sort_field: "modified",
		sort_dir: "desc",
		start,
		page_length,
	});
// `file` (the upload's docname) pins the exact File; file_url is the fallback.
export const dropFile = (file_url, file_name, file) =>
	call("jarvis.chat.filebox.drop_file", {
		file_url,
		...(file_name ? { file_name } : {}),
		...(file ? { file } : {}),
	});

// ── Write approvals (the write-safety gate) ─────────────────────────────────
// A tool that would change ERP data is parked server-side and announced as an
// `action:pending` event carrying a one-time token. confirm_tool is the ONLY
// path that runs the parked call. `source` is an optional provenance tag
// (layered re-check design): the on-demand controls pass "pill"/"menu" and the
// silent auto-heal passes "auto", so the backend attributes per layer how
// often a card had to be re-surfaced (AC-detect rescue).
export const listPendingConfirmations = (conversation, source) =>
	call("jarvis.chat.actions_api.list_pending_confirmations", {
		conversation: conversation || "",
		...(source ? { source } : {}),
	});
export const confirmTool = (token, conversation) =>
	call("jarvis.chat.actions_api.confirm_tool", { token, conversation: conversation || "" });
// "Approve & run" (skill approve-and-run, P1) - the sibling of confirm_tool that
// also opens the armed skill's uninterrupted run. Same envelope as confirmTool.
// Reachable only from a card whose preview.card.approve_run is true.
export const approveAndRun = (token, conversation) =>
	call("jarvis.chat.actions_api.approve_and_run", { token, conversation: conversation || "" });
// Deny (P0c): consumes the token, leaves a durable "discarded" receipt chip,
// and fires no agent turn - mirrors the desktop SPA's dismissTool. DecisionSheet
// gates this behind its own "Discard this action?" confirm.
export const dismissTool = (token, conversation) =>
	call("jarvis.chat.actions_api.dismiss_tool", { token, conversation: conversation || "" });

// ── Draft writes (the ```jarvis-action``` card) ─────────────────────────────
// The other half of the write story, and the one the phone was missing entirely:
// for create/update the agent does NOT call a tool — it PROPOSES a document and
// waits for a human to apply it. Without this the agent could describe the record
// it wanted to make and the user had no button to make it.
//
// apply_action runs the write as the session user (so the tool's own permission
// and protected-field checks fire unchanged) and leaves a receipt in the chat.
// The form meta is needed because the agent's card names fields by LABEL, while
// the write wants fieldnames.
export const getDoctypeFormMeta = (doctype) =>
	call("jarvis.chat.actions_api.get_doctype_form_meta", { doctype });
// Saved values + child rows (each with its `name`) of one document, write-gated.
export const loadDoc = (doctype, name) =>
	call("jarvis.chat.actions_api.load_doc", { doctype, name });
export const applyAction = (action) =>
	call("jarvis.chat.actions_api.apply_action", { action: JSON.stringify(action) });
// Cancel an unsaved draft for good: the server records it, so a reload (or the web
// app) no longer shows the card. `message` is the DRAFT row's name.
export const dismissDraft = (conversation, message) =>
	call("jarvis.chat.actions_api.dismiss_draft", { conversation, message });

// ── Server draft cards (the shared draft host's calls) ──────────────────────
// The shared form builder and draft host (@shared/lib/draftModel.js, toolDraftHost.js) call
// these by the desktop SPA's names and argument order, so the phone passes them straight in.
// Same endpoints, methods and serialisation as the desktop's api.js.
// `loadDocForEdit` is `loadDoc` under the name the shared builder uses (one implementation).
export const loadDocForEdit = loadDoc;
// The hidden values a child table's row rules read that Desk fetches from a link:
// {ok, values: {link field: {link value: {rule field: value}}}}.
export const ruleValues = (doctype, table, links) =>
	call("jarvis.chat.actions_api.rule_values", {
		doctype,
		table,
		links: JSON.stringify(links),
	});
// Save an edited SERVER draft card: `values` is only what the person changed. The server runs the
// edited call itself and the card ends as the decided row, or stays Pending when the reply says
// `card_open`. Same envelopes as applyAction.
export const saveDraftCard = (token, conversation, values) =>
	call("jarvis.chat.actions_api.save_draft_card", {
		token,
		conversation: conversation || "",
		values: JSON.stringify(values || {}),
	});
// The edit form's link picker: Frappe's own search_link. The optional context is sent only when known.
export const searchLink = (doctype, txt, pageLength, referenceDoctype, linkFieldname, filters) => {
	const args = { doctype, txt: txt || "", page_length: pageLength || 8 };
	if (referenceDoctype) args.reference_doctype = referenceDoctype;
	if (linkFieldname) args.link_fieldname = linkFieldname;
	if (filters?.length) args.filters = filters;
	return call("frappe.desk.search.search_link", args);
};
// Park the confirm card for a create receipt's suggested next step (Submit). Nothing runs until Confirm.
export const proposeNextAction = (conversation, step) =>
	call("jarvis.chat.actions_api.propose_next_action", {
		conversation,
		doctype: step.doctype,
		name: step.name,
		kind: step.kind,
		...(step.action ? { action: step.action } : {}),
	});

// ── Attachments ────────────────────────────────────────────────────────────
// Standard Frappe upload, private by default: a chat attachment is the user's
// document, not a public asset. Same shape the SPA's uploadFile uses.
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
	if (!r.ok) {
		// status and serverReason let uploadFailureCopy say what to do (too large, sign in again).
		const err = new Error(`Couldn't upload ${file.name} (${r.status})`);
		err.status = r.status;
		err.serverReason = serverReason(await r.json().catch(() => null));
		throw err;
	}
	const data = await r.json();
	const f = data.message || data;
	return { file_url: f.file_url, file_name: f.file_name || file.name, name: f.name };
}

// Dictation goes through the SPA's module unchanged: same endpoint, same
// 150-second client budget, same error unwrapping. The mic is a place where two subtly
// different clients would be two subtly different bugs.
export { transcribeAudio } from "@shared/api/voice.js";

// F01 recovery: a check only reads the scoped receipt, never resends a message.
export const sendRecoverableMessage = (request) =>
	call("jarvis.chat.send_requests.send_message", {
		request_id: request.id,
		conversation: request.conversation,
		message: request.text,
		attachments: JSON.stringify(
			request.attachments.map((a) => ({ file_url: a.file_url, file_name: a.name }))
		),
		approval_tokens: JSON.stringify(request.approvalTokens),
		...(request.model ? { model_override: request.model } : {}),
		...(request.thinking ? { thinking_override: request.thinking } : {}),
		...(request.autoMode ? { auto_mode: 1 } : {}),
	});
export const checkDelivery = (requestId) =>
	call("jarvis.chat.send_requests.check_delivery", { request_id: requestId });

// Reuse the desktop admission contract; these endpoints enforce ownership.
export const activeQueuedTurn = (conversation) =>
	call("jarvis.chat.admission.active_turn_for_conversation", { conversation });
export const queuePosition = (run_id) => call("jarvis.chat.admission.queue_position", { run_id });
export const cancelQueuedTurn = (run_id) =>
	call("jarvis.chat.admission.cancel_queued_turn", { run_id });

// The failed reply's error (secrets removed), model and run id, for the Report error ticket
// draft. Resolves {ok, data}; rejects without support access.
export const getErrorReportDetails = (conversation, message) =>
	call("jarvis.support.api.error_report_details", { conversation, message });
