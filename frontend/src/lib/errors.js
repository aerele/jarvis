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
export function errMessage(e) {
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
	// cannot explain.
	if (!specific && e && (e.status === 401 || e.status === 403)) {
		return "Your session has expired. Please sign in again.";
	}
	const raw = specific || "Something went wrong. Please try again.";
	if (typeof document === "undefined") return raw;
	const d = document.createElement("div");
	d.innerHTML = raw; // decodes &gt; &amp; &#39; etc; detached, so no script/img runs
	return (d.textContent || d.innerText || raw).trim();
}
