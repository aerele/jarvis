// Compose-side body helpers, shared by the two SupportComposer hosts (the
// new-ticket page and the thread reply). The RENDER-side counterpart is
// lib/supportHtml.js (renderSupportHtml) — that rewrites+sanitizes INCOMING
// message HTML for display; these prepare OUTGOING body HTML before it is sent.
// Kept as pure functions (no toast, no Vue) so both hosts behave identically
// and the logic is unit-testable on its own.
import DOMPurify from "dompurify";

// TipTap emits "<p></p>" for an empty document, so a blank editor is NOT the
// empty string. Real emptiness = no text once tags are stripped AND non-breaking
// spaces are removed: a stray "<p>&nbsp;</p>" (or a literal U+00A0 from a space
// the user typed then deleted around) LOOKS blank yet would otherwise arm Submit.
// Inline images are disabled on this surface (they are routed to attachments), so
// the body is text-only — no <img> exception is needed here, and omitting it means
// a stray errored-upload placeholder can't masquerade as content.
export function supportBodyIsEmpty(html) {
	return !String(html || "")
		.replace(/<[^>]*>/g, "")
		.replace(/&nbsp;|&#160;|&#xa0;/gi, "")
		.replace(/\u00a0/g, "")
		.trim();
}

// Strip inline data:/blob: images before sending, THEN DOMPurify LAST (never
// store un-sanitized markup). TipTap can leave a data:/blob: <img> behind from an
// HTML/Word paste or a failed inline-upload placeholder; Helpdesk's own server-side
// nh3 sanitize drops a data: src anyway (so the agent would see a broken image) and
// blob: URLs are per-session. Images belong in the attach flow, not the body.
// Returns { html, stripped } — the caller decides whether to notify the user, so
// this stays free of any UI dependency.
export function cleanSupportBody(html) {
	const doc = new DOMParser().parseFromString(html || "", "text/html");
	let stripped = 0;
	// Iterate every <img> and TRIM the src before testing the scheme: a browser
	// trims leading/trailing whitespace in a URL, so `src=" data:…"` still renders
	// yet an `[src^="data:"]` selector would miss it — leaking megabytes of base64
	// downstream with no "removed" toast. Trim + lowercase + startsWith closes that.
	for (const img of doc.querySelectorAll("img")) {
		const src = (img.getAttribute("src") || "").trim().toLowerCase();
		if (src.startsWith("data:") || src.startsWith("blob:")) {
			img.remove();
			stripped += 1;
		}
	}
	return { html: DOMPurify.sanitize(doc.body.innerHTML), stripped };
}

// One-call submit-body prep for a host's create()/send(): clean, then decide
// emptiness on the CLEANED html (a body that was only a data:-image becomes an
// empty doc after stripping, so it must read as empty, not as "<p><img></p>").
// Returns { body, stripped } — body is "" for an empty result (the host decides
// what empty means: reject on new-ticket, synthesize a files-only note on the
// thread); `stripped` lets the host toast that inline images were dropped.
export function prepareSupportBody(html) {
	const { html: cleaned, stripped } = cleanSupportBody(html);
	return { body: supportBodyIsEmpty(cleaned) ? "" : cleaned, stripped };
}
