// Pure display helpers for the wiki write-back review panel, kept out of the
// SFC so the label/excerpt logic is unit-testable without mounting the page.

// A 403 from a reviewer-gated endpoint means "you are not a reviewer" — the
// panel hides rather than showing an error to a non-reviewer.
export function isPermissionDenied(e) {
	return !!e && (e.status === 403 || e.exc_type === "PermissionError");
}

// One-line identity for a proposal row: the page slug (or title) + its type.
export function proposalHeadline(p) {
	const pv = (p && p.preview) || {};
	const name = pv.slug || pv.title || p?.title || "wiki page";
	return pv.page_type ? `${name} · ${pv.page_type}` : name;
}

// The FULL proposed append body a reviewer must read before approving. It is
// NOT truncated — a benign head + malicious/incorrect tail is exactly what the
// review gate defends against, so the whole body is shown (the panel scrolls it).
export function proposalBody(p) {
	return (((p && p.preview) || {}).append_md || "").trim();
}

// True when the body is long enough that the reviewer must scroll to read it all
// — the panel shows a "scroll to review the full note" caption in that case.
export function isLongBody(p, threshold = 600) {
	return proposalBody(p).length > threshold;
}

// Human dropper label with a clear fallback (the row always carries a dropper,
// but a since-deleted user reads as unknown rather than a blank).
export function dropperLabel(p) {
	return (p && (p.dropper_name || p.dropper)) || "unknown";
}
