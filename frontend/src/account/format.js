// Pure display helpers for the account/billing section. No Vue, no I/O - unit-tested.
const STATUS_LABELS = {
	Active: "Active",
	"Pending Verification": "Pending verification",
	Cancelled: "Cancelled",
	Expired: "Expired",
};
export function statusLabel(status, cancelAtPeriodEnd) {
	// A plan scheduled to end still reports status "Active" server-side (the
	// customer keeps full access until the period ends), so "Active" would be
	// technically true and practically misleading. Say what is happening.
	if (cancelAtPeriodEnd) return "Cancelling";
	if (!status) return "Unknown";
	return STATUS_LABELS[status] || status;
}
// Pill colour for a subscription status. Lives here (not inline in the pane)
// so the cancelling branch is unit-tested like its siblings.
export function pillTone(status, cancelAtPeriodEnd) {
	if (cancelAtPeriodEnd) return "jv-pill-warn";
	if (status === "Active") return "jv-pill-ok";
	if (
		status === "Cancelled" ||
		status === "Past Due" ||
		status === "Pending Payment" ||
		status === "Pending Verification"
	)
		return "jv-pill-warn";
	if (status === "Expired") return "jv-pill-bad";
	return "jv-pill-muted";
}
// pillTone speaks the legacy jv-pill-* class names, which surfaces that still
// render those classes depend on, so the frappe-ui Badge theme is derived here
// rather than by changing pillTone (design.md §3.8 status map).
//
// Both billing surfaces - the page and the settings pane - read this one map.
// They used to carry a verbatim copy each, which is how the same subscription
// could show green on one and grey on the other after a single-sided edit.
const BADGE_THEME = {
	"jv-pill-ok": "green",
	"jv-pill-warn": "orange",
	"jv-pill-bad": "red",
	"jv-pill-muted": "gray",
};
export function statusBadgeTheme(status, cancelAtPeriodEnd) {
	return BADGE_THEME[pillTone(status, cancelAtPeriodEnd)] || "gray";
}
// The cancel button's label adapts to what the customer actually has: an
// autopay mandate is an auto-renewal to switch off; a one-shot plan is a
// subscription to end. Promising to "cancel auto-renewal" on a plan that
// never auto-renews would be a lie.
export function cancelActionLabel(hasMandate) {
	return hasMandate ? "Cancel auto-renewal" : "Cancel subscription";
}
// Short "D MMM" date for pills/inline chips. "2026-08-21 12:00" -> "21 Aug".
export function shortDate(dt) {
	const s = (dt || "").trim().split(" ")[0];
	const m = s.match(/^(\d{4})-(\d{2})-(\d{2})$/);
	if (!m) return "";
	const MONTHS = [
		"Jan",
		"Feb",
		"Mar",
		"Apr",
		"May",
		"Jun",
		"Jul",
		"Aug",
		"Sep",
		"Oct",
		"Nov",
		"Dec",
	];
	return `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]}`;
}
// Pill text while a cancellation is scheduled: a glanceable end date, not the
// ambiguous "Cancelling" (which reads as an in-progress operation). The plan is
// still Active until then; the date is what the customer actually needs.
export function cancelPillLabel(accessEndsOn) {
	const d = shortDate(accessEndsOn);
	return d ? `Ends ${d}` : "Ending";
}
// Banner copy while a cancellation is scheduled.
export function cancellationNotice(accessEndsOn) {
	const end = (accessEndsOn || "").trim();
	const date = end ? end.split(" ")[0] : "";
	if (!date) return "Your plan is scheduled to end. You keep full access until then.";
	return `Your plan ends on ${date}. You keep full access until then.`;
}
// Shared INR formatter - the single place amounts get localized.
export function inr(n) {
	return `₹${(Number(n) || 0).toLocaleString("en-IN")}`;
}
// Big price line on a plan card: "₹3,999".
export function planAmount(priceInr) {
	return inr(Number(priceInr) || 0);
}
// Small muted per-cycle suffix next to the amount: "/yr" for annual, "/mo"
// for everything else. Every plan is paid, so this is never blank.
export function planSuffix(priceInr, billingCycle) {
	return (billingCycle || "").toLowerCase() === "annual" ? "/yr" : "/mo";
}
export function planPriceLabel(priceInr, billingCycle) {
	const suffix = planSuffix(priceInr, billingCycle);
	return `${planAmount(priceInr)} / ${suffix.slice(1)}`;
}
// Cycle line under the price ("Billed monthly" / "Billed annually"), keyed off
// the shared suffix helper so the cycle rule cannot drift from the price.
export function planCycleLabel(p) {
	const suffix = planSuffix(p && p.price_inr, p && p.billing_cycle);
	const trial = Number(p && p.trial_days) || 0;
	const billed = suffix === "/yr" ? "Billed annually" : "Billed monthly";
	// Auto-pay trial: nothing is charged until the trial ends, then autopay begins.
	return trial > 0 ? `${trial}-day free trial, then ${billed.toLowerCase()}` : billed;
}
// A plan's feature bullets, whatever shape the field arrived in.
//
// The billing page renders plans from TWO sources in one grid and they do not
// agree: get_account's own plan carries features as a JSON array (or its
// stringified form), while the rows in upgrade_plans / downgrade_plans carry
// the raw newline-separated Data field, which is what the onboarding plan step
// parses. Driving one grid from two parsers is how a card silently loses its
// bullets, so both shapes resolve here.
export function planFeatures(p) {
	const f = p && p.features;
	if (Array.isArray(f)) return f.filter(Boolean);
	const s = typeof f === "string" ? f.trim() : "";
	if (!s) return [];
	if (s.startsWith("[")) {
		try {
			const parsed = JSON.parse(s);
			if (Array.isArray(parsed)) return parsed.filter(Boolean);
		} catch (e) {
			// Not valid JSON after all - fall through and treat it as text.
		}
	}
	return s
		.split(/\r?\n/)
		.map((x) => x.trim())
		.filter(Boolean);
}
export function renewalLabel(currentPeriodEnd, daysRemaining) {
	const end = (currentPeriodEnd || "").trim();
	if (!end) return "No active period";
	const date = end.split(" ")[0];
	const d = Number(daysRemaining) || 0;
	// A past-due / expired subscription reports 0 or negative days; don't render a
	// nonsensical "Renews <past date> · -12 days left". #200 review #11.
	if (d <= 0) return `Expired ${date}`;
	return `Renews ${date} · ${d} day${d === 1 ? "" : "s"} left`;
}

// Billing lifecycle banner, or null when there is nothing to say. The server
// sends BOTH audiences' wording because it authenticates the customer, not the
// individual - only the bench knows who is looking, so the pick happens here.
const BILLING_TONE = { expiring: "info", grace: "warning", expired: "warning" };
const BILLING_TITLE = {
	expiring: "Plan ending soon",
	grace: "Payment overdue",
	expired: "Chat is paused",
};

export function billingBanner(notice, canRenew) {
	const phase = (notice && notice.phase) || "";
	if (!BILLING_TONE[phase]) return null;
	const message = (canRenew ? notice.admin_message : notice.member_message) || "";
	if (!message) return null;
	return {
		phase,
		type: BILLING_TONE[phase],
		title: BILLING_TITLE[phase],
		message,
		// Renewing is the admin's action; a member has nothing to click.
		showRenew: !!canRenew,
		// Only the pre-expiry nudge is dismissible. Grace is the last window in
		// which paying still helps, and expired already blocks chat.
		dismissible: phase === "expiring",
	};
}
