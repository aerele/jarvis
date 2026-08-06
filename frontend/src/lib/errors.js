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
// that, with no route forward but a Retry that failed identically (#696). A
// TypeError/ReferenceError/SyntaxError reaching here is always that kind of
// leak, so its message is never read.
function isInternalCrash(e) {
	return e instanceof TypeError || e instanceof ReferenceError || e instanceof SyntaxError;
}

// Frappe HTML-escapes throw() messages before they reach the client, so a
// backend "Settings -> Developer" arrives here as "Settings -&gt; Developer"
// and would render literally if shown as-is. Decode entities + strip any
// wrapping tags via a detached element (never inserted into the live DOM, so
// nothing in the message - script/img/etc. - ever executes) before handing
// the string to a caller.
export function errMessage(e) {
	// A 401/403 is the single most likely cause of an error this formatter
	// cannot otherwise explain (an expired session, a logged-out tab, a
	// permission change) - name it plainly and give the customer somewhere to
	// go, rather than whatever generic string the server happened to send.
	if (!isInternalCrash(e) && e && (e.status === 401 || e.status === 403)) {
		return "Your session has expired. Please sign in again.";
	}
	const raw =
		(!isInternalCrash(e) && e && ((e.messages && e.messages[0]) || e.message)) ||
		"Something went wrong. Please try again.";
	if (typeof document === "undefined") return raw;
	const d = document.createElement("div");
	d.innerHTML = raw; // decodes &gt; &amp; &#39; etc; detached, so no script/img runs
	return (d.textContent || d.innerText || raw).trim();
}
