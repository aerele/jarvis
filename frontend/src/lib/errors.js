import { cookieUser } from "./sessionCookie.js";

// Shared extractor for a user-facing message out of a Frappe API error.
// Single source for AccountView / OnboardingView / LlmPoolEditor so a change to
// Frappe's error envelope only has to be made once.
//
// MUST be total (#696): given ANY shape of `e`, this returns a sentence and
// never throws. frappe-ui's own call() (src/utils/call.js) can itself crash
// mid-parse - it does `try { error = JSON.parse(response) } catch {}` and then
// reads `error.exc_type` on the very next line with no guard, so a response
// body that fails to parse as JSON leaves `error` undefined and THROWS a raw
// TypeError instead of the Frappe-shaped Error call() normally builds. That
// TypeError's own .message ("Cannot read properties of undefined (reading
// 'exc_type')") is an implementation detail, never something to show a
// customer - a 403 on an unauthenticated `list_plans` call rendered exactly
// that, with no route forward but a Retry that failed identically (#696).
//
// Matched on the SPECIFIC V8 crash shape, not the TypeError class (round-4
// review F2): a blanket `instanceof TypeError` also swallowed the browser's
// own `TypeError: Failed to fetch` for a real network failure - text a
// support engineer relies on - which regressed the prior invariant that
// e.message is shown whenever present. This regex is what a property read on
// undefined/null actually throws: current V8 wording ("Cannot read
// properties of undefined (reading 'exc_type')") and the pre-2021 form
// ("Cannot read property 'exc_type' of undefined") both match.
const INTERNAL_CRASH_MESSAGE = /^Cannot read propert(y|ies) (?:'[^']*' )?of (undefined|null)\b/;
export function isInternalCrash(e) {
	return e instanceof TypeError && INTERNAL_CRASH_MESSAGE.test((e && e.message) || "");
}

// The sentence shown when the error carries nothing specific. Exported so a
// caller never has to spell it out to detect it: an earlier pass at #699 gave
// each site a custom fallback by comparing errMessage()'s RESULT against this
// literal, which put 6 fresh copies of the string into components - the exact
// duplication #699 exists to remove, and one that would have failed silently
// (falling back to the generic sentence) the day the wording changed. Pass the
// custom sentence in as `fallback` instead.
export const GENERIC_ERROR_MESSAGE =
	"Something went wrong. Try again, and check back if it keeps happening.";

export const SESSION_EXPIRED_MESSAGE = "Your session has expired. Please sign in again.";

// An expired session (#644): Frappe answers 403 "not whitelisted ... Login to
// access" (a PermissionError) AND clears the cookies, which is why the cookie
// is the signal (frappe-ui drops the body's `session_expired` flag). So a 401/403 that arrives while the
// cookie now reads Guest/absent is an expired session, not a permission error.
// A 403 on a live session stays a genuine permission error.
export function isSessionExpired(e) {
	if (!e) return false;
	if (typeof document === "undefined") return false;
	const authFailure = e.status === 401 || e.status === 403 || e.exc_type === "PermissionError";
	return authFailure && !cookieUser();
}

// Only what the server said ON PURPOSE (frappe.throw / _server_messages), or "".
// Never e.message: frappe-ui composes that from the method name, the exception
// class and the traceback head, which is not a sentence for a customer. For a
// caller (lib/sendDelivery.js) that builds its own cause sentence from the
// transport facts and only wants to quote the server when it actually spoke.
export function serverSentMessage(e) {
	const first = e && Array.isArray(e.messages) ? e.messages[0] : "";
	return typeof first === "string" ? first : "";
}

export function errMessage(e, fallback = GENERIC_ERROR_MESSAGE) {
	if (isSessionExpired(e)) return SESSION_EXPIRED_MESSAGE;
	// The server's OWN explicit message always wins, even on a 401/403 (round-4
	// review F1): frappe.throw("You do not have permission to disconnect this
	// model") is a real, actionable remedy, and burying it under a blanket
	// "session expired" sentence sends the customer through a re-auth into the
	// SAME 403 for a reason expiry never caused - a misdiagnosis loop that is
	// arguably worse than the crash this file was written to fix.
	const specific = !isInternalCrash(e) && e && ((e.messages && e.messages[0]) || e.message);
	// Only once there is nothing specific to show does a 401/403 get named
	// plainly, since an expired session / logged-out tab / permission change is
	// still the single most likely cause of an error this formatter otherwise
	// cannot explain. This outranks `fallback` deliberately: "sign in again" is
	// an actionable remedy, a caller's "Could not save." is not.
	if (!specific && e && (e.status === 401 || e.status === 403)) {
		return SESSION_EXPIRED_MESSAGE;
	}
	return (typeof specific === "string" && messageText(specific)) || messageText(fallback);
}

// Frappe HTML-escapes throw() messages before they reach the client, so a backend
// "Settings -> Developer" arrives as "Settings -&gt; Developer" and would render
// literally if shown as-is. Decode entities and drop tags by parsing into an inert
// document (DOMParser: no script runs and no image loads, which a detached
// element's innerHTML does not guarantee). Text only, and bounded in length.
// The caps are guards against an absurd payload, not formatting: a long validation
// message is shown whole.
export const MAX_MESSAGE_CHARS = 4000;
const MAX_SOURCE_CHARS = 50000;
const MAX_PAYLOAD_CHARS = 200000;

export function messageText(raw) {
	const src = String(raw ?? "").slice(0, MAX_SOURCE_CHARS);
	if (typeof DOMParser === "undefined") return clip(src.trim());
	const doc = new DOMParser().parseFromString(src, "text/html");
	// A message that is nothing but markup has no text: the caller's fallback speaks.
	return clip((doc.body.textContent || "").trim());
}

function clip(text) {
	return text.length > MAX_MESSAGE_CHARS ? text.slice(0, MAX_MESSAGE_CHARS - 1) + "…" : text;
}

// The first message in a response's `_server_messages` (a JSON array of JSON
// strings), or "". Never throws, whatever the payload looks like.
export function firstServerMessage(raw) {
	if (typeof raw !== "string" || raw.length > MAX_PAYLOAD_CHARS) return "";
	try {
		const list = JSON.parse(raw);
		const first = Array.isArray(list) ? list[0] : list;
		const entry = typeof first === "string" ? JSON.parse(first) : first;
		return entry && typeof entry.message === "string" ? entry.message : "";
	} catch {
		return "";
	}
}

// errMessage() returns PLAIN TEXT: it decodes entities so a text sink renders
// "Settings -> Developer" rather than "Settings -&gt; Developer". That decode is
// unsafe in an HTML sink, and frappe-ui's Toast is one - Toast.vue binds its
// `message` prop with `v-html`. So the round trip through a toast is:
//
//   frappe.throw(user_value)  ->  server escapes once  ->  "&lt;img onerror=...&gt;"
//   errMessage()              ->  decodes              ->  "<img onerror=...>"
//   toast.error(...)          ->  v-html re-parses     ->  a LIVE <img> element
//
// The escaping that made the value safe is exactly what errMessage() removes,
// so the plain text has to be re-escaped on its way into an HTML sink. Use this
// for toast.*() and any other v-html binding; use errMessage() for `{{ }}`,
// textContent, and ChatView's own notify(), which interpolate as text.
// The single escape implementation in the frontend. pages/skills/escapeHtml.js
// re-exports this as `esc`, which is the name the skills review flow already
// imports at its own v-html sink (frappe-ui's ConfirmDialog).
const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export function escapeHtml(v) {
	// One pass over the string, so the "&" rewritten for "<" is never re-escaped.
	return String(v ?? "").replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]);
}

export function errHtml(e, fallback) {
	return escapeHtml(errMessage(e, fallback));
}

// One implementation for desktop, Desk, and mobile; Python uses its rule data.
export {
	turnErrorInfo,
	subscriptionUpstreamFromError,
} from "../../../jarvis/public/js/turn_errors.mjs";
