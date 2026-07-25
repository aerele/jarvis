// HTML-escape helper (SAR-1 defense-in-depth, security review). frappe-ui's
// ConfirmDialog renders its `message` via v-html, so ANY untrusted value
// interpolated into a confirm message — a requester-authored wiki page title,
// a skill name, a target role, or a warning string built from those — MUST be
// escaped before it reaches that sink, or a stored `<img onerror=…>` runs in
// the reviewer's privileged session on Approve. Pure + importable so it is
// node-tested (the promotionBudget.js precedent) and reused verbatim at every
// call site. Server-side neutralization of the wiki title is the second belt.
export function esc(v) {
	return String(v ?? "")
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;")
		.replace(/'/g, "&#39;");
}
