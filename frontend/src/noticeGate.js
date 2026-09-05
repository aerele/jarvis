// Release notice, delivered by the www/jarvis.py boot payload as
// window.release_notice = {active, version, message, tier, behind,
// banner_interval_days}. The hard gate keys off `active`; the customer-facing
// version pill + soft banner (Slice 3b) derive their display state from
// `version` + `tier` + `behind` via the shared, node-testable `releaseNudge.js`.
import { computed, ref } from "vue";
import { call } from "frappe-ui";
import { bannerShouldShow, writeSnooze, readSnooze } from "@/releaseNudge";
import { session } from "@/data/session";

const n = window.release_notice || {};

// Namespace the snooze by the signed-in user (same source globalNotifier reads),
// so two people sharing a browser profile keep independent snoozes; guest -> "anon".
const userId = session.user || "anon";

// The boot payload is stable for the page's lifetime, so this is a plain object
// (not reactive): pillFor()/bannerShouldShow() read it, and it never changes.
export const notice = {
	active: !!n.active,
	version: (n.version || "").trim(),
	message: n.message || "",
	tier: n.tier || "none",
	behind: Number(n.behind) || 0,
	banner_interval_days: Number(n.banner_interval_days) || 7,
};

const cleared = ref(false);
export const checking = ref(false);

// Hard gate: no dismiss. It lifts only when the control plane stops serving the
// notice (this tenant updated, or the operator retired it).
export const showNotice = computed(() => notice.active && !cleared.value);

// ---- Soft banner (snooze-per-device) ---------------------------------------
// The snooze lives in localStorage; this ref mirrors it so the banner hides
// reactively the moment it is snoozed. Seeded from storage at boot (a read-throw
// reads as not-snoozed, so the banner shows - see readSnooze()).
const snooze = ref(readSnooze(userId));

// Reactive: soft tier AND not currently snoozed. `Date.now()` is read at
// evaluation time; the only reactive dependency is `snooze`, so snoozing (which
// replaces the ref below) recomputes this to false and the banner hides.
export const showBanner = computed(() => bannerShouldShow(notice, Date.now(), snooze.value));

// Dismiss the soft banner: persist the snooze (best-effort) AND update the ref
// in-memory so `showBanner` flips to false immediately. The in-memory update
// mirrors writeSnooze()'s payload exactly, so a localStorage write-throw still
// hides the banner this session (the snooze just isn't persisted and returns
// next boot - the accepted edge behaviour, spec §6).
export function snoozeBanner() {
	const now = Date.now();
	writeSnooze(notice, now, userId);
	const days = notice.banner_interval_days || 7;
	snooze.value = { until: now + days * 86400000, version: notice.version };
}

// ---- What's-new dialog (shared open state) ---------------------------------
// The pill, the soft banner, and the hard gate all open the same What's-new
// panel. The gate replaces the whole app (AppShell renders it in place of the
// router view), so ChatView and the gate each mount their own <WhatsNewDialog>;
// this single ref keeps whichever one is live driven by all three triggers.
export const whatsNewOpen = ref(false);
export function openWhatsNew() {
	whatsNewOpen.value = true;
}

// Boot reads a mirror that may predate the tenant's update, and an open tab never
// re-reads it at all, so the gate re-pulls from admin itself.
export async function recheck() {
	if (checking.value) return;
	checking.value = true;
	try {
		const fresh = await call("jarvis.release_notice.check");
		if (fresh && !fresh.active) {
			cleared.value = true;
			window.location.reload();
		}
	} catch (e) {
		/* offline or admin unreachable - keep the gate up and retry later */
	} finally {
		checking.value = false;
	}
}
