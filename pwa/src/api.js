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

const CHAT = "jarvis.chat.api.";

export const listConversations = () => call(CHAT + "list_conversations");
export const getConversation = (conversation) => call(CHAT + "get_conversation", { conversation });
export const archiveConversation = (conversation) =>
	call(CHAT + "archive_conversation", { conversation });
export const renameConversation = (conversation, title) =>
	call(CHAT + "rename_conversation", { conversation, title });
export const setStar = (conversation, starred) =>
	call(CHAT + "set_star", { conversation, starred: starred ? 1 : 0 });
export const setAutoApply = (conversation, value) =>
	call(CHAT + "set_auto_apply", { conversation, value: value ? 1 : 0 });

// Model name for the chat header, the model/effort pickers, and whether the mic
// is allowed to appear (STT is off unless the admin configured a transcription
// key).
export const getChatUiSettings = () => call(CHAT + "get_chat_ui_settings");

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
	{ attachments = [], model, thinking, approvalTokens } = {}
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
	});

export const stopRun = (conversation, runId) =>
	call(CHAT + "stop_run", { conversation, run_id: runId || "" });

// A message's rich outputs (charts, generated images, files). `get_conversation`
// returns the item list on `message.canvas`; this fetches one item's body.
export const getCanvas = (message, name = "", dark = 0) =>
	call(CHAT + "get_canvas", { message, name, dark });

// Render-ready preview of a tabular/text artifact (xlsx/csv → sheets, txt → text).
export const previewFile = (file_url) => call(CHAT + "preview_file", { file_url });

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
export const dropFile = (file_url, file_name) =>
	call("jarvis.chat.filebox.drop_file", { file_url, ...(file_name ? { file_name } : {}) });

// ── Write approvals (the write-safety gate) ─────────────────────────────────
// A tool that would change ERP data is parked server-side and announced as an
// `action:pending` event carrying a one-time token. confirm_tool is the ONLY
// path that runs the parked call. There is no deny endpoint by design: dropping
// the card leaves the token to expire, which is exactly what "no" means.
export const listPendingConfirmations = (conversation) =>
	call("jarvis.chat.actions_api.list_pending_confirmations", {
		conversation: conversation || "",
	});
export const confirmTool = (token, conversation) =>
	call("jarvis.chat.actions_api.confirm_tool", { token, conversation: conversation || "" });

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
export const applyAction = (action) =>
	call("jarvis.chat.actions_api.apply_action", { action: JSON.stringify(action) });

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
	if (!r.ok) throw new Error(`Couldn't upload ${file.name} (${r.status})`);
	const data = await r.json();
	const f = data.message || data;
	return { file_url: f.file_url, file_name: f.file_name || file.name };
}

// Dictation goes through the SPA's module unchanged: same endpoint, same 25s
// client cap, same error unwrapping. The mic is a place where two subtly
// different clients would be two subtly different bugs.
export { transcribeAudio } from "@shared/api/voice.js";
