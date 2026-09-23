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

// The proposed append body, trimmed to a preview length (never mid-run huge).
export function proposalExcerpt(p, max = 400) {
	const body = (((p && p.preview) || {}).append_md || "").trim();
	if (body.length <= max) return body;
	return body.slice(0, max).trimEnd() + "…";
}

// Human dropper label with a clear fallback (the row always carries a dropper,
// but a since-deleted user reads as unknown rather than a blank).
export function dropperLabel(p) {
	return (p && (p.dropper_name || p.dropper)) || "unknown";
}
