// Shared release-nudge display logic for both frontends: the SPA imports it as
// `@/releaseNudge`, the mobile PWA as `@shared/releaseNudge` (one-way share, see
// pwa/vite.config.js). Keep this a pure ES module - no `@/` imports and no Vue - so
// `node --test` can resolve and run it without a bundler.
//
// The wire payload is minimal: { active, version, message, tier, behind,
// banner_interval_days }. There is NO `state` field - the display state (current /
// soft / severe / hard / unknown) is derived here from `version` + `tier` + `behind`.
// Only "hard" is blocking (active=true, critical release or below the floor
// version); "severe" (behind >= release_lag_threshold) is a red pill + banner
// like "soft", non-blocking - see the Slice 3b.1 severity-split addendum.

export const SNOOZE_KEY = "jarvis-release-banner-snooze";

// The version pill's tone and (stable) label for a boot-payload notice. The pill
// is a NUDGE: it renders only when a newer version exists. There is no positive
// "on the latest" all-clear badge - being up to date is the quiet default, not a
// pill (an always-green "on the latest" badge was noise on every current chat).
//   - no notice / no target version -> hidden.
//   - tier "none" (a known version we're level with) -> hidden (up to date).
//   - tier "hard" -> red   (N versions behind, or "Update required"). "hard" is
//     block-only (critical release / below the floor version) and is normally
//     hidden behind the full-page gate, so in practice the visible red pill is
//     "severe" below - both render identically.
//   - tier "severe" -> red (N versions behind, or "Update available"; NON-blocking:
//     behind >= release_lag_threshold, chat stays open).
//   - tier "soft" OR any unknown-but-versioned future tier -> amber (N versions
//     behind, or "Update available"). Amber is the safe default for "there's a
//     newer version, urgency unknown": a nudge, never a false all-clear.
export function pillFor(notice) {
	// Hidden when there's nothing to nudge about: no notice, no target version, or
	// we're level with a known version (tier "none"). Up to date is the quiet
	// default - there is no positive "on the latest" badge.
	if (!notice || !notice.version || notice.tier === "none") return { show: false };
	const behind = Number(notice.behind) || 0;
	if (notice.tier === "hard") {
		return {
			show: true,
			tone: "red",
			label:
				behind >= 1
					? `${behind} version${behind === 1 ? "" : "s"} behind`
					: "Update required",
		};
	}
	if (notice.tier === "severe") {
		return {
			show: true,
			tone: "red",
			label:
				behind >= 1
					? `${behind} version${behind === 1 ? "" : "s"} behind`
					: "Update available",
		};
	}
	// soft OR any unknown-but-versioned future tier -> amber. Forward-compatible:
	// a tier this build doesn't recognise still nudges (amber), never a false
	// green "on the latest".
	return {
		show: true,
		tone: "amber",
		label:
			behind >= 1
				? `${behind} version${behind === 1 ? "" : "s"} behind`
				: "Update available",
	};
}

// The banner's tone, single-sourced from pillFor so the pill and the banner can
// never disagree on colour - whatever pillFor would paint the pill, the banner
// matches ("amber"/"red"). In practice only called once bannerShouldShow has
// cleared the notice (soft/severe/unknown, all versioned), so it returns "amber"
// or "red"; a "none" (up-to-date) notice has no pill and so no tone (undefined),
// but never reaches here. The SPA maps "red" -> Banner type="error", else
// "warning", and both frontends map the tone to their jv-tone-* class.
export function bannerToneFor(notice) {
	return pillFor(notice).tone;
}

// The banner shows for any versioned tier that is NOT the all-clear ("none") or
// the blocking full-page gate ("hard") - so soft, severe, AND any unknown future
// tier (which pillFor colours amber). Mirrors pillFor: it fires exactly when the
// pill would be amber/red, never green and never the "hard" full-page path. Shows
// only when not currently snoozed: no snooze, a snooze for a different (older)
// target version, or a snooze that has expired. `now` and `snooze` are passed in
// so this stays pure and testable.
export function bannerShouldShow(notice, now, snooze) {
	if (!notice || !notice.version) return false;
	if (notice.tier === "none" || notice.tier === "hard") return false;
	return !snooze || snooze.version !== notice.version || now > snooze.until;
}

// Per-user, per-device snooze state. The key is namespaced by `userId` so two
// people sharing a browser profile don't inherit each other's snooze (a missing
// userId falls back to a stable "anon" bucket). `userId` is a PARAMETER, not an
// import, so this module stays node-testable and single-sourced - the caller
// passes the current user in.
//
// Reads/writes are wrapped so a private-mode / quota / absent localStorage never
// throws: a read-throw reads as not-snoozed (banner shows), a write-throw is
// swallowed (the dismiss animation still completes; snooze just doesn't persist
// and the banner returns next boot - acceptable).
function snoozeKey(userId) {
	return `${SNOOZE_KEY}:${userId || "anon"}`;
}

export function readSnooze(userId) {
	if (typeof localStorage === "undefined") return null;
	try {
		return JSON.parse(localStorage.getItem(snoozeKey(userId))) || null;
	} catch {
		return null;
	}
}

export function writeSnooze(notice, now, userId) {
	if (typeof localStorage === "undefined") return;
	try {
		const days = notice.banner_interval_days || 7;
		localStorage.setItem(
			snoozeKey(userId),
			JSON.stringify({ until: now + days * 86400000, version: notice.version })
		);
	} catch {
		// swallow - a write failure must not break the dismiss animation.
	}
}
