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
function isInternalCrash(e) {
	return e instanceof TypeError && INTERNAL_CRASH_MESSAGE.test((e && e.message) || "");
}

// Frappe HTML-escapes throw() messages before they reach the client, so a
// backend "Settings -> Developer" arrives here as "Settings -&gt; Developer"
// and would render literally if shown as-is. Decode entities + strip any
// wrapping tags via a detached element (never inserted into the live DOM, so
// nothing in the message - script/img/etc. - ever executes) before handing
// the string to a caller.
// The sentence shown when the error carries nothing specific. Exported so a
// caller never has to spell it out to detect it: an earlier pass at #699 gave
// each site a custom fallback by comparing errMessage()'s RESULT against this
// literal, which put 6 fresh copies of the string into components - the exact
// duplication #699 exists to remove, and one that would have failed silently
// (falling back to the generic sentence) the day the wording changed. Pass the
// custom sentence in as `fallback` instead.
export const GENERIC_ERROR_MESSAGE = "Something went wrong. Please try again.";

export function errMessage(e, fallback = GENERIC_ERROR_MESSAGE) {
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
		return "Your session has expired. Please sign in again.";
	}
	const raw = specific || fallback;
	if (typeof document === "undefined") return raw;
	const d = document.createElement("div");
	d.innerHTML = raw; // decodes &gt; &amp; &#39; etc; detached, so no script/img runs
	return (d.textContent || d.innerText || raw).trim();
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

// ---------------------------------------------------------------------------
// Chat TURN failures (#702, reworked by #823): the counterpart to errMessage()
// above, for a chat turn's `error` column instead of a Frappe API exception.
//
// #702 gave the turn error a code. #823 gives it a `retryable` boolean and a
// place to live. Before #823 the SPA offered Retry on every code except
// `cancelled`, so a revoked key, a model that does not exist and an exhausted
// quota all got a button that could not possibly work; and the classification
// was never persisted, so the same failure could read one way live and another
// way after a refresh.
//
// The server now attaches an envelope - {code, retryable, resets_in_seconds} -
// to the live run:error event AND to the message row, so both paths render
// identically. This file owns only the COPY for each code (the seam rule from
// the Error & Copy Book: the backend supplies codes, the frontend supplies
// sentences). The retryable column below is the same table the backend keeps in
// jarvis/chat/error_taxonomy.py, and jarvis/chat/turn_error_codes.json is the
// contract both are tested against, so the three cannot drift the way the old
// hand-synced copies did (#757, #760).
//
// classifyTurnErrorCode below survives for ONE case: a message row written
// before #823, which has an error string and no code. It is the last resort,
// not the normal path.
//
// MUST be total, same contract as errMessage above: given ANY shape of
// input, this never throws.
//
// Copy follows the Error & Copy Book: title, one sentence, one action; no em
// dashes; and a terminal failure says what to do instead of offering a retry.
// `memberHint`/`action` exist for codes only an admin can fix - a member is told
// who to ask rather than shown a button the server would refuse.
// Exported for the parity test only: errors.test.js asserts this table against
// jarvis/chat/turn_error_codes.json, the contract the Python taxonomy is tested
// against too.
export const TURN_ERROR_CODES = {
	// --- terminal: retrying cannot help, so the card offers the real remedy ---
	cancelled: {
		retryable: false,
		headline: "This message was cancelled.",
		// No hint: a cancelled turn renders as a muted status note, never the red
		// error card.
		hint: "",
	},
	"quota-exhausted": {
		retryable: false,
		headline: "Your plan hit a limit.",
		// The Error & Copy Book's canonical wording for this state. There is no
		// Retry because clicking one changes nothing until the limit resets; the
		// customer can still send a fresh message once they have dealt with it.
		hint: "Check your plan, then try again.",
		memberHint: "Ask your administrator to check the plan.",
		action: "billing",
	},
	"auth-invalid": {
		retryable: false,
		headline: "Your model connection was rejected.",
		hint: "Reconnect the model in Settings.",
		memberHint: "Ask your administrator to reconnect the model.",
		action: "settings",
	},
	"model-not-found": {
		retryable: false,
		headline: "That model is not available.",
		hint: "Pick a different model in Settings.",
		memberHint: "Ask your administrator to pick another model.",
		action: "settings",
	},
	"context-overflow": {
		retryable: false,
		headline: "This chat is too long.",
		hint: "Start a new chat, or ask for less at once.",
	},
	"agent-unpaired": {
		retryable: false,
		headline: "Your workspace needs reconnecting.",
		// The book's pattern for a "needs a person" state: one sentence saying who
		// is on it, plus a real Contact support button rather than the words
		// "contact support" sitting inert in the prose. The action drops to nothing
		// on a build where support is not available, and the hint still says a
		// human is handling it, so the card is never a silent dead end.
		hint: "Our team has been notified.",
		action: "support",
	},
	// --- retryable ---
	unreachable: {
		retryable: true,
		headline: "I couldn't reach the assistant.",
		hint: "Check your connection, then try again.",
	},
	timeout: {
		retryable: true,
		headline: "That took too long.",
		hint: "Try again, or ask for less at once.",
	},
	throttled: {
		retryable: true,
		headline: "The model is busy right now.",
		hint: "Try again in a moment.",
	},
	"recovery-expired": {
		retryable: true,
		headline: "This took too long, so I stopped.",
		hint: "Send your message again.",
	},
	gateway: {
		retryable: true,
		headline: "A temporary problem interrupted this.",
		hint: "Try sending your message again.",
	},
	internal: {
		retryable: true,
		headline: "Something went wrong.",
		hint: "Try again. If it keeps happening, contact support.",
	},
	// Pre-#823 code for "busy / quota / billing", now split into `throttled` and
	// `quota-exhausted`. Nothing produces it any more; it stays so an in-flight
	// event or an externally supplied code still renders a sentence.
	provider: {
		retryable: true,
		headline: "The model is busy right now.",
		hint: "Try again in a moment.",
	},
};

// A reset clock the provider named, as a phrase a person reads. Deliberately
// coarse: the exact second is noise, and "about" keeps it honest when the
// provider's own clock drifts.
function formatResetWait(seconds) {
	const s = Number(seconds);
	if (!Number.isFinite(s) || s <= 0) return "";
	if (s < 90) return "a moment";
	if (s < 3600) return `about ${Math.round(s / 60)} minutes`;
	// Days matter: a monthly quota reset is a real value the server will persist
	// (it accepts a clock up to 30 days), and "about 720 hours" is not a sentence
	// anyone should read.
	if (s < 36 * 3600) {
		const hours = Math.round(s / 3600);
		return hours === 1 ? "about an hour" : `about ${hours} hours`;
	}
	const days = Math.round(s / 86400);
	return days === 1 ? "about a day" : `about ${days} days`;
}

// The text ladder, mirroring jarvis/chat/error_taxonomy.py tier for tier so a
// pre-#823 row classifies here exactly as the server would. The marker lists
// live in jarvis/chat/turn_error_codes.json, which BOTH suites assert their own
// copy against: the three hand-synced ladders are what shipped #757 and #760,
// and a fixture both sides are tested on is what stops a fourth.
//
// EXHAUSTED (a definitive refusal with an hours-scale reset clock - terminal)
// versus THROTTLED (transient back-pressure - worth retrying) follows the host
// plane's own rule, so the two planes cannot disagree about the same provider.
const OVERFLOW_MARKERS = [
	"maximum context length",
	"context_length_exceeded",
	"context length exceeded",
	"prompt is too long",
	"prompt too large",
	"too many tokens",
	"reduce the length of the messages",
];
// Every entry is an unambiguous slug or a vendor's verbatim exhaustion sentence,
// never a bare English word: Gemini writes "You exceeded your current quota" for
// an ordinary per-minute throttle as readily as for a spent balance, so a bare
// "quota" here would strand a customer on a failure a retry would have fixed. An
// ambiguous 429 falls through to the status check and reads as `throttled`.
// Terminal is the expensive verdict and has to be earned.
const EXHAUSTED_MARKERS = [
	"usage_limit_reached",
	"model_cooldown",
	"usage_limit",
	"insufficient_quota",
	"insufficient_balance",
	"insufficient balance",
	"insufficient credit",
	"insufficient funds",
	"credit balance is too low",
	"billing_hard_limit",
	"out of credits",
];
const MODEL_MARKERS = [
	"model_not_found",
	"model not found",
	"unknown model",
	"does not exist or you do not have access",
	"is not a valid model",
	"unsupported model",
	"no such model",
];
const AUTH_MARKERS = [
	"invalid_api_key",
	"invalid api key",
	"incorrect api key",
	"authentication_error",
	"authentication failed",
	"unauthorized",
	"api key not valid",
	"no api key found",
	"permission_denied",
	"subscription_rejected",
	"credential",
];
const THROTTLE_MARKERS = [
	"rate_limit_exceeded",
	"rate limit",
	"rate-limit",
	"ratelimit",
	"too many requests",
	"overloaded",
	"capacity",
	"cooldown",
];
const UNREACHABLE_MARKERS = ["ws open failed", "unreachable", "connection timed out"];
const TIMEOUT_MARKERS = ["timed out", "timeout", "deadline"];

// The vendor's own HTTP status, written the two ways vendors write it. A bare
// three-digit number anywhere in the text is NOT read as a status: that would
// take a token count or a model name for one.
const STATUS_PAREN = /\((\d{3})\)/;
const STATUS_WORD = /\b(?:http[ _-]?)?(?:status|error)[ _-]?code[:= ]\s*(\d{3})\b/i;
const STATUS_CODES = {
	401: "auth-invalid",
	402: "quota-exhausted",
	403: "auth-invalid",
	404: "model-not-found",
	429: "throttled",
};

// Exported for the parity test only, alongside TURN_ERROR_CODES.
export const TURN_ERROR_MATCHERS = {
	markers: {
		overflow: OVERFLOW_MARKERS,
		exhausted: EXHAUSTED_MARKERS,
		model: MODEL_MARKERS,
		auth: AUTH_MARKERS,
		throttle: THROTTLE_MARKERS,
		unreachable: UNREACHABLE_MARKERS,
		timeout: TIMEOUT_MARKERS,
	},
	http_status: STATUS_CODES,
};

function classifyTurnErrorCode(raw) {
	// String(raw ?? "") rather than `raw || ""`: a truthy non-string (a number,
	// an object) would otherwise reach .toLowerCase() below and throw, which
	// classifyErrorCode (the function this replaced) did not guard against.
	const low = String(raw ?? "").toLowerCase();
	const has = (markers) => markers.some((k) => low.includes(k));
	// Phase-0 admission cancel markers: a queued turn cancelled by the user or
	// aged out by the system leaves a durable transcript marker so a later
	// reload shows WHY there's no reply (not a silent drop). Classified as
	// "cancelled" so it renders as a muted note, not a red "something went
	// wrong".
	if (
		low.startsWith("you cancelled this message") ||
		low.startsWith("waited too long in the queue")
	)
		return "cancelled";
	// Mirrors the worker's own explicit code="internal" backstop
	// (turn_handler.py's last-resort `except Exception`) so a reload, which
	// only has the persisted string, classifies it the same way the live
	// event did - not as the "gateway" default below.
	if (low.startsWith("unexpected worker error")) return "internal";

	// Structure the provider put in the sentence. Markers run before the status
	// so a 429 that names a usage limit reads as exhausted (terminal) rather than
	// as ordinary back-pressure - the distinction the next action turns on.
	if (has(OVERFLOW_MARKERS)) return "context-overflow";
	if (has(EXHAUSTED_MARKERS)) return "quota-exhausted";
	if (has(MODEL_MARKERS)) return "model-not-found";
	if (has(AUTH_MARKERS)) return "auth-invalid";
	if (has(THROTTLE_MARKERS)) return "throttled";
	const m = STATUS_PAREN.exec(low) || STATUS_WORD.exec(low);
	if (m && STATUS_CODES[m[1]]) return STATUS_CODES[m[1]];

	// The legacy keyword tier. "connection timed out" is a transport failure (we
	// could not reach the gateway), not the model taking too long, so it stays
	// ahead of the timeout markers - the Python ladder orders it the same way, or
	// one string would read as unreachable live and timeout on a reload.
	if (has(UNREACHABLE_MARKERS)) return "unreachable";
	if (low.includes("recovery window")) return "recovery-expired";
	if (has(TIMEOUT_MARKERS)) return "timeout";
	// #702: the agent's own mid-run failure text (e.g. "LLM request failed:
	// network connection error.", relayed verbatim) is not a reliable signal
	// that the network was actually the problem - see the module comment
	// above. A run that got far enough to be accepted and start is
	// presumptively a transient fault on the gateway/container side, not the
	// unreachable/provider cases already matched above, and not a bug in our
	// own code (that path stamps "internal" explicitly and never reaches this
	// fallback). Defaulting here tells the customer to retry instead of the
	// unhelpful "something went wrong".
	return "gateway";
}

// Reset seconds off a live event or a persisted row, whichever shape arrived.
function resetsFrom(meta) {
	const v = meta && (meta.resets_in_seconds ?? meta.resetsInSeconds);
	const n = Number(v);
	return Number.isFinite(n) && n > 0 ? n : 0;
}

// Combined, TOTAL turn-error envelope:
//   {code, headline, hint, retryable, action, resetsInSeconds}
//
// `meta` is the server's envelope - a live run:error event's
// {code, retryable, resets_in_seconds}, or the same three read off a persisted
// message row. A bare string is accepted too, which is what the pre-#823 call
// shape passed. Envelope first: a server-supplied code always wins over
// re-classifying `raw`, and a server-supplied `retryable` always wins over the
// table, because the server saw the raised exception and the gateway's own
// rejection code and this file only ever sees prose.
//
// `opts.canConfigure` says whether this user can act on an ADMIN remedy - the
// model settings and the plan page are both admin-only, server-side. A member
// who cannot is told who to ask instead of being shown a button the server would
// refuse. `opts.canContactSupport` does the same for the support desk, which
// some builds do not have at all. Both default to true so a caller that passes
// no options gets the full-capability card.
export function turnErrorInfo(raw, meta, opts) {
	try {
		const m = typeof meta === "string" ? { code: meta } : meta || {};
		const code = m.code || classifyTurnErrorCode(raw);
		// An unrecognised wire code (a future server taxonomy value this build does
		// not know yet) degrades to the generic headline with NO hint: inventing a
		// remedy for a code we cannot name would be worse than saying nothing. It
		// stays retryable for the same reason the server's guess tier does - the
		// one verdict never to reach for on no evidence is "this can never work".
		const entry = TURN_ERROR_CODES[code] || {
			retryable: true,
			headline: TURN_ERROR_CODES.internal.headline,
			hint: "",
		};
		const canConfigure = !opts || opts.canConfigure !== false;
		const canContactSupport = !opts || opts.canContactSupport !== false;
		// A remedy the user cannot perform is not a remedy. Swap in the "who to
		// ask" wording where there is one and drop the button, rather than render a
		// dead end. Both `settings` and `billing` are admin-only server-side, which
		// is why they gate together - the plan page's own Renew button already
		// hides from a member for exactly this reason.
		const gated =
			(entry.action === "settings" || entry.action === "billing") && !canConfigure
				? true
				: entry.action === "support" && !canContactSupport;
		const resetsInSeconds = resetsFrom(m);
		let hint = gated && entry.memberHint ? entry.memberHint : entry.hint || "";
		// Name the wait when the provider named it. "Try again in a moment" is
		// true but useless when the real answer is forty minutes, and a terminal
		// limit is a lot easier to accept with a time on it.
		const wait = code === "cancelled" ? "" : formatResetWait(resetsInSeconds);
		if (wait) hint = entry.retryable ? `Try again in ${wait}.` : `It resets in ${wait}.`;
		return {
			code,
			headline: entry.headline || TURN_ERROR_CODES.internal.headline,
			hint,
			// `retryable` is what the Retry button is gated on. The server's word
			// wins; the table answers for a legacy row that has no envelope.
			retryable: typeof m.retryable === "boolean" ? m.retryable : !!entry.retryable,
			action: gated ? null : entry.action || null,
			resetsInSeconds,
		};
	} catch {
		const fallback = TURN_ERROR_CODES.internal;
		return {
			code: "internal",
			headline: fallback.headline,
			hint: "",
			retryable: true,
			action: null,
			resetsInSeconds: 0,
		};
	}
}
