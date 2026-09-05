// Shared release-nudge display logic for both frontends: the SPA imports it as
// `@/releaseNudge`, the mobile PWA as `@shared/releaseNudge` (one-way share, see
// pwa/vite.config.js). Keep this a pure ES module - no `@/` imports and no Vue - so
// `node --test` can resolve and run it without a bundler.
//
// The wire payload is minimal: { active, version, message, tier, behind,
// banner_interval_days }. There is NO `state` field - the display state (current /
// soft / hard / unknown) is derived here from `version` + `tier` + `behind`.

export const SNOOZE_KEY = "jarvis-release-banner-snooze";

// The version pill's tone and (stable) label for a boot-payload notice.
//
// `agentName` is a PARAMETER (default "Jarvis"), not an import, so this module stays
// node-testable and single-sourced - the caller passes the branded agent name in.
//   - no notice / no target version -> hidden (never a false "on the latest").
//   - tier "hard" -> red   (N versions behind, or "Update required").
//   - tier "soft" -> amber (N versions behind, or "Update available").
//   - otherwise (tier "none", with a known version) -> green (current).
export function pillFor(notice, agentName = "Jarvis") {
	if (!notice || !notice.version) return { show: false };
	const behind = Number(notice.behind) || 0;
	if (notice.tier === "hard") {
		return {
			show: true,
			tone: "red",
			label: behind >= 1 ? `${behind} versions behind` : "Update required",
		};
	}
	if (notice.tier === "soft") {
		return {
			show: true,
			tone: "amber",
			label: behind >= 1 ? `${behind} versions behind` : "Update available",
		};
	}
	return { show: true, tone: "green", label: `On the latest ${agentName}` };
}

// The soft banner shows only for the soft tier, and only when not currently snoozed:
// no snooze, a snooze for a different (older) target version, or a snooze that has
// expired. `now` and `snooze` are passed in so this stays pure and testable.
export function bannerShouldShow(notice, now, snooze) {
	if (!notice || !notice.version || notice.tier !== "soft") return false;
	return !snooze || snooze.version !== notice.version || now > snooze.until;
}

// Per-device snooze state. Reads/writes are wrapped so a private-mode / quota / absent
// localStorage never throws: a read-throw reads as not-snoozed (banner shows), a
// write-throw is swallowed (the dismiss animation still completes; snooze just doesn't
// persist and the banner returns next boot - acceptable).
export function readSnooze() {
	if (typeof localStorage === "undefined") return null;
	try {
		return JSON.parse(localStorage.getItem(SNOOZE_KEY)) || null;
	} catch {
		return null;
	}
}

export function writeSnooze(notice, now) {
	if (typeof localStorage === "undefined") return;
	try {
		const days = notice.banner_interval_days || 7;
		localStorage.setItem(
			SNOOZE_KEY,
			JSON.stringify({ until: now + days * 86400000, version: notice.version })
		);
	} catch {
		// swallow - a write failure must not break the dismiss animation.
	}
}
