// "Proposed <relative time>" on a chat action card that has waited over an hour:
// cards never expire (decision 2), so an old one says how old it is. `created_at`
// is epoch seconds from the server (no timezone guessing).
//
// The SPA and PWA are separate builds, so this file is mirrored in pwa/src/lib.

export const PROPOSED_LABEL_AFTER_S = 3600;

export function proposedLabel(createdAt, nowMs = Date.now()) {
	const t = Number(createdAt);
	if (!createdAt || !Number.isFinite(t)) return "";
	const age = Math.floor(nowMs / 1000 - t);
	if (age < PROPOSED_LABEL_AFTER_S) return "";
	const hours = Math.floor(age / 3600);
	if (hours < 24) return `Proposed ${hours}h ago`;
	return `Proposed ${Math.floor(hours / 24)}d ago`;
}
