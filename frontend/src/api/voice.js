// Voice API - thin wrappers around `jarvis.chat.voice` and
// `jarvis.chat.voice_notes_api` (wiki endpoints live in src/api/wiki.js).
// Mirrors src/api/learning.js: one `call` per endpoint. The exception is
// `transcribeAudio`, which must POST a multipart body (the recorded blob), so
// it uses a raw fetch like src/api.js uploadFile instead of frappe-ui `call`.
import { call } from "frappe-ui";

const VN = "jarvis.chat.voice_notes_api.";
export const TRANSCRIPTION_TIMEOUT_MS = 150000;

// Server maps the blob MIME to the model's audio format; the filename only
// helps werkzeug pick a sane extension.
function _audioFilename(blob) {
	const t = (blob && blob.type) || "";
	if (t.includes("ogg")) return "audio.ogg";
	if (t.includes("mp4")) return "audio.m4a";
	if (t.includes("wav")) return "audio.wav";
	if (t.includes("mpeg") || t.includes("mp3")) return "audio.mp3";
	return "audio.webm";
}

// Pull the human reason out of a Frappe error response so the caller's
// notifyActionError shows more than "(417)".
function _frappeErr(data, status) {
	try {
		if (data && data._server_messages) {
			const msgs = JSON.parse(data._server_messages);
			const first = typeof msgs[0] === "string" ? JSON.parse(msgs[0]) : msgs[0];
			if (first && first.message) return String(first.message).replace(/<[^>]*>/g, "");
		}
		if (data && data.exception) return String(data.exception).split(":").pop().trim();
	} catch (e) {
		/* fall through to the status code */
	}
	return `transcription failed (${status})`;
}

// Recorded blob → verbatim transcript. Returns {ok, text, stt_ms, model}.
//
// timeoutMs is the per-call budget. It covers the browser upload plus the server's
// 10-second connect and 90-second provider read budgets. Every desktop and PWA
// recorder inherits the same default; the retained chat dictation path may retry a
// rejected recording once after a backoff. An optional signal lets the caller cancel
// an in-flight upload on teardown.
export async function transcribeAudio(
	blob,
	{ durationS = 0, timeoutMs = TRANSCRIPTION_TIMEOUT_MS, signal } = {},
) {
	const fd = new FormData();
	fd.append("audio", blob, _audioFilename(blob));
	fd.append("duration_s", String(Math.round(durationS || 0)));
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), timeoutMs);
	// Compose an external cancel signal (component unmount) with the timeout abort.
	const onExternalAbort = () => ctrl.abort();
	if (signal) {
		if (signal.aborted) ctrl.abort();
		else signal.addEventListener("abort", onExternalAbort);
	}
	let r;
	try {
		r = await fetch("/api/method/jarvis.chat.voice.transcribe_audio", {
			method: "POST",
			headers: { "X-Frappe-CSRF-Token": window.csrf_token || "" },
			body: fd,
			credentials: "include",
			signal: ctrl.signal,
		});
	} catch (e) {
		if (signal && signal.aborted) throw new Error("transcription cancelled");
		if (ctrl.signal.aborted) throw new Error("transcription timed out");
		throw e;
	} finally {
		clearTimeout(timer);
		if (signal) signal.removeEventListener("abort", onExternalAbort);
	}
	let data = null;
	try {
		data = await r.json();
	} catch (e) {
		/* non-JSON error body */
	}
	if (!r.ok) throw new Error(_frappeErr(data, r.status));
	return (data && data.message) || data || {};
}

// ── voice notes (Business tab + chat nudge share these) ──────────────────────
// p: {transcript, context_type, conversation, entities (JSON string),
//     duration_s, source} - server defaults context_type "Business" /
// source "Business Tab" when omitted. → {name}
export const saveVoiceNote = (p = {}) => call(VN + "save_voice_note", p);
export const listMyVoiceNotesPage = (p = {}) =>
	call(VN + "list_my_voice_notes_page", {
		start: p.start || 0,
		page_length: p.page_length || 20,
		...(p.status ? { status: p.status } : {}),
		...(p.search ? { search: p.search } : {}),
	});
// owner-only, status "New" only - edited transcript re-feeds the daily sweep
export const updateVoiceNote = (name, transcript) =>
	call(VN + "update_voice_note", { name, transcript });
export const deleteVoiceNote = (name) => call(VN + "delete_voice_note", { name });
// {stt_enabled, my_notes, org_new_notes (SM only), last_processed_at,
//  last_process_status, can_process}
export const getBusinessStatus = () => call(VN + "get_business_status");
// SM-only → {ok: true} | {ok: false, reason}
export const processVoiceNotesNow = () => call(VN + "process_voice_notes_now");

// ── wiki ─────────────────────────────────────────────────────────────────────
// Wiki wrappers moved to src/api/wiki.js with the Wiki tab; the nudge dismiss
// is re-exported so ChatView's `voice.dismissWikiNudge` keeps working.
export { dismissWikiNudge } from "./wiki";
