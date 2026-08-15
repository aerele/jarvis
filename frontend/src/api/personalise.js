// Personalise API - thin wrappers around `jarvis.chat.personalise_api.*`, the
// endpoints behind the Skills-page "Personalise" tab (questions bank, notes
// capture, and the admin-only Personalisation Settings). Mirrors src/api/voice.js
// and src/api/learning.js: one flat-kwargs `call` per endpoint. Dict payloads
// (settings, question rules) are JSON-encoded the way the server `_parse_json`s
// them - same convention as learning.js `setLearningSettings`.
//
// This module is the single home for EVERY personalise_api binding (DESIGN 6b),
// including ones this file's own Personalise tab doesn't call: the Notes view
// (F3) and the Personalisation Settings dialog (F4) import their endpoints from
// here, so all of them live in one place.
import { call } from "frappe-ui";

const PA = "jarvis.chat.personalise_api.";

// ── probe + composer chrome ──────────────────────────────────────────────────
// Desk-user gate (403 for guest/portal). →
// {personalise, wiki, analysis, review, stt_enabled, unanswered_count,
//  personalise_enabled}. Also the SkillsPage tab-visibility probe.
export const getSkillsAreaCaps = () => call(PA + "get_skills_area_caps");

// ── questions ────────────────────────────────────────────────────────────────
// Owner-scoped question bank. Envelope {rows, total, has_more, start,
// page_length}; rows: {name, question, origin, status, context_md, created,
// answered_at, has_answer, source_pattern}. status maps Unanswered/Answered/
// Ignored; sort newest|oldest|origin.
export const listQuestionsPage = (p = {}) =>
	call(PA + "list_questions_page", {
		status: p.status || "Unanswered",
		search: p.search || "",
		sort: p.sort || "newest",
		start: p.start || 0,
		page_length: p.page_length || 20,
	});

// The caller's own single question (owner-only), same row shape as a
// list_questions_page row (incl. context_md/status) so the SPA can rehydrate an
// "Answering:" panel from a re-answer hand-off without paging the whole list.
// Rejects with exc_type "DoesNotExistError" when the row is missing OR soft-
// Deleted (both mean "no longer available"), "PermissionError" for a non-owner.
export const getQuestion = (name) => call(PA + "get_question", { name });

// Answer a question from ANY status. Creates a Note (kind derived server-side:
// url→Link, attachment→Attachment, duration_s>0→Voice, else Text), links it,
// stamps Answered, enqueues immediate ingest. → {ok, note, question_status}.
export const answerQuestion = (p = {}) =>
	call(PA + "answer_question", {
		name: p.name,
		text: p.text || "",
		url: p.url || "",
		attachment: p.attachment || "",
		duration_s: p.duration_s || 0,
	});

// "Not now" - stays listed and answerable. → {ok}
export const ignoreQuestion = (name) => call(PA + "ignore_question", { name });
// Soft-delete (audit row kept so the generator won't re-mint it). → {ok}
export const deleteQuestion = (name) => call(PA + "delete_question", { name });

// ── notes ────────────────────────────────────────────────────────────────────
// Free capture (no question). Same 4-kind payload / kind derivation as
// answer_question; keeps save_voice_note untouched for chat-nudge compat.
// → {ok, note}
export const saveNote = (p = {}) =>
	call(PA + "save_note", {
		text: p.text || "",
		url: p.url || "",
		attachment: p.attachment || "",
		duration_s: p.duration_s || 0,
		source: p.source || "Personalise",
	});

// Owner-scoped notes list (answered-question notes + free-capture notes).
// Envelope idiom; kind/status quick-filters + wildcard-escaped search.
export const listNotesPage = (p = {}) =>
	call(PA + "list_notes_page", {
		kind: p.kind || "",
		status: p.status || "",
		search: p.search || "",
		start: p.start || 0,
		page_length: p.page_length || 20,
	});

// Note detail incl. question back-ref + the wiki pages the note produced.
// → {..., question, question_text, wiki_pages:[{slug, title}]}
export const getNote = (name) => call(PA + "get_note", { name });
// Owner-only delete (aliases delete_voice_note server-side). → {ok}
export const deleteNote = (name) => call(PA + "delete_note", { name });

// ── personalisation settings (admin set: SM | Administrator | Jarvis Admin) ───
// → {daily_question_cap, personalise_enabled}
export const getPersonalisationSettings = () => call(PA + "get_personalisation_settings");
// payload: {daily_question_cap, personalise_enabled} - JSON-encoded like
// setLearningSettings so the server `_parse_json`s one arg.
export const setPersonalisationSettings = (payload = {}) =>
	call(PA + "set_personalisation_settings", { payload: JSON.stringify(payload || {}) });

// Admin-only: run the chat-transcript question miner immediately over the recent
// backlog. Returns {ok, reason?}.
export const generateChatQuestionsNow = () => call(PA + "generate_chat_questions_now");

// ── question rules (admin set) ───────────────────────────────────────────────
// Admin-only: desk Role names selectable as a rule's target_role (Role scope) -
// enabled desk roles minus Administrator/Guest/All, sorted. Purpose-built for
// this admin screen so a Jarvis Admin who is NOT a System Manager still gets a
// populated Role picker (unlike the Wiki tab's narrower manageable_roles). → [str]
export const listRoleOptions = () => call(PA + "list_role_options");
// Admin-authored questions materialized per in-scope user. → {rows:[...]}
export const listQuestionRules = () => call(PA + "list_question_rules");
// rule payload: {name?, question, context_md, scope, target_role, target_user,
// active} - JSON-encoded (dict arg). → {ok, name}
export const saveQuestionRule = (payload = {}) =>
	call(PA + "save_question_rule", { payload: JSON.stringify(payload || {}) });
export const deleteQuestionRule = (name) => call(PA + "delete_question_rule", { name });
