import { test } from "node:test";
import assert from "node:assert/strict";
import {
	statusLabel,
	pillTone,
	planPriceLabel,
	renewalLabel,
	inr,
	inrExact,
	planAmount,
	planSuffix,
	planHasGst,
	cancelActionLabel,
	cancellationNotice,
	shortDate,
	cancelPillLabel,
	billingBanner,
} from "./format.js";

test("statusLabel: maps known states, passes through unknown", () => {
	assert.equal(statusLabel("Active"), "Active");
	assert.equal(statusLabel("Pending Verification"), "Pending verification");
	assert.equal(statusLabel(""), "Unknown");
	assert.equal(statusLabel(null), "Unknown");
});
test("planPriceLabel: monthly, annual", () => {
	assert.equal(planPriceLabel(100, "Monthly"), "₹100 / mo");
	assert.equal(planPriceLabel(1000, "Annual"), "₹1,000 / yr");
});
test("inr: localizes amounts, coerces junk to 0", () => {
	assert.equal(inr(0), "₹0");
	assert.equal(inr(3999), "₹3,999");
	assert.equal(inr(150000), "₹1,50,000");
	assert.equal(inr(null), "₹0");
	assert.equal(inr("abc"), "₹0");
});
// inrExact: same localisation as inr(), but a fractional amount (GST math can
// yield paise precision) always renders at 2dp instead of inr()'s bare
// toLocaleString, which would show "₹4,100.5" - correct in value, but
// inconsistent next to a whole-rupee row and easy to misread as rounded off.
test("inrExact: a fractional amount always renders at 2dp", () => {
	assert.equal(inrExact(4100.5), "₹4,100.50");
	assert.equal(inrExact(625.5), "₹625.50");
});
test("inrExact: a whole-rupee amount renders exactly like inr(), no decimals", () => {
	assert.equal(inrExact(4130), "₹4,130");
	assert.equal(inrExact(0), "₹0");
	assert.equal(inrExact(3999), "₹3,999");
});
test("inrExact: junk coerces to ₹0, same as inr()", () => {
	assert.equal(inrExact(null), "₹0");
	assert.equal(inrExact("abc"), "₹0");
});
test("planHasGst: true only when gst_percent is a positive number", () => {
	assert.equal(planHasGst({ gst_percent: 18 }), true);
	assert.equal(planHasGst({ gst_percent: "18" }), true); // stringified API row
});
test("planHasGst: false for 0, absent, undefined or missing gst_percent - never claims 'excl. GST' without one", () => {
	assert.equal(planHasGst({ gst_percent: 0 }), false);
	assert.equal(planHasGst({ gst_percent: "0" }), false);
	assert.equal(planHasGst({}), false); // pre-companion-PR get_plans row: field absent
	assert.equal(planHasGst({ gst_percent: undefined }), false);
	assert.equal(planHasGst(null), false);
});
test("planAmount: INR amount, zero/junk coerces to ₹0", () => {
	assert.equal(planAmount(0), "₹0");
	assert.equal(planAmount(null), "₹0");
	assert.equal(planAmount(100), "₹100");
	assert.equal(planAmount(3999), "₹3,999");
});
test("planSuffix: /yr for annual, /mo otherwise", () => {
	assert.equal(planSuffix(100, "Monthly"), "/mo");
	assert.equal(planSuffix(100, ""), "/mo");
	assert.equal(planSuffix(1000, "Annual"), "/yr");
	assert.equal(planSuffix(1000, "annual"), "/yr");
});
test("renewalLabel: renders days remaining; handles empty/zero", () => {
	assert.equal(renewalLabel("2026-08-01 00:00:00", 30), "Renews 2026-08-01 · 30 days left");
	assert.equal(renewalLabel("2026-08-01 00:00:00", 1), "Renews 2026-08-01 · 1 day left");
	assert.equal(renewalLabel("", 0), "No active period");
});
test("renewalLabel: expired/past-due (<= 0 days) shows Expired, not negative days", () => {
	assert.equal(renewalLabel("2026-06-01 00:00:00", -12), "Expired 2026-06-01");
	assert.equal(renewalLabel("2026-06-01 00:00:00", 0), "Expired 2026-06-01");
});

test("statusLabel: a scheduled cancellation reads Cancelling, not Active", () => {
	// The server keeps status Active through the paid period, so without this
	// branch a cancelling plan would render a reassuring green "Active".
	assert.equal(statusLabel("Active", 1), "Cancelling");
	assert.equal(statusLabel("Active", 0), "Active");
	assert.equal(statusLabel("Active"), "Active");
});

test("pillTone: cancelling warns; otherwise tracks status", () => {
	assert.equal(pillTone("Active", 1), "jv-pill-warn");
	assert.equal(pillTone("Active", 0), "jv-pill-ok");
	assert.equal(pillTone("Expired", 0), "jv-pill-bad");
	assert.equal(pillTone("Past Due", 0), "jv-pill-warn");
	assert.equal(pillTone("", 0), "jv-pill-muted");
});

test("cancelActionLabel: only promises auto-renewal when one exists", () => {
	assert.equal(cancelActionLabel(true), "Cancel auto-renewal");
	assert.equal(cancelActionLabel(false), "Cancel subscription");
	assert.equal(cancelActionLabel(undefined), "Cancel subscription");
});

test("cancellationNotice: names the end date, degrades without one", () => {
	assert.equal(
		cancellationNotice("2026-08-20 16:11:36.216083"),
		"Your plan ends on 2026-08-20. You keep full access until then."
	);
	assert.match(cancellationNotice(""), /keep full access until then/);
	assert.match(cancellationNotice(null), /keep full access until then/);
});

test("shortDate: D MMM, degrades on junk", () => {
	assert.equal(shortDate("2026-08-21 12:56:09"), "21 Aug");
	assert.equal(shortDate("2026-01-05"), "5 Jan");
	assert.equal(shortDate(""), "");
	assert.equal(shortDate(null), "");
	assert.equal(shortDate("not-a-date"), "");
});

test("cancelPillLabel: glanceable end date, not the ambiguous 'Cancelling'", () => {
	assert.equal(cancelPillLabel("2026-08-21 12:56:09"), "Ends 21 Aug");
	assert.equal(cancelPillLabel(""), "Ending");
	assert.equal(cancelPillLabel(null), "Ending");
});

const _notice = (phase) => ({
	phase,
	admin_message: "ADMIN COPY",
	member_message: "MEMBER COPY",
});

test("billingBanner: says nothing without a usable phase", () => {
	assert.equal(billingBanner(null, true), null);
	assert.equal(billingBanner({}, true), null);
	assert.equal(billingBanner({ phase: "active" }, true), null);
});

test("billingBanner: picks the wording for whoever is looking", () => {
	assert.equal(billingBanner(_notice("expiring"), true).message, "ADMIN COPY");
	assert.equal(billingBanner(_notice("expiring"), false).message, "MEMBER COPY");
});

test("billingBanner: only offers Renew to someone who can renew", () => {
	assert.equal(billingBanner(_notice("expired"), true).showRenew, true);
	assert.equal(billingBanner(_notice("expired"), false).showRenew, false);
});

test("billingBanner: only the pre-expiry nudge is dismissible", () => {
	// Grace is the last window in which paying still helps; expired blocks chat.
	assert.equal(billingBanner(_notice("expiring"), true).dismissible, true);
	assert.equal(billingBanner(_notice("grace"), true).dismissible, false);
	assert.equal(billingBanner(_notice("expired"), true).dismissible, false);
});

test("billingBanner: tone escalates across the lifecycle", () => {
	assert.equal(billingBanner(_notice("expiring"), true).type, "info");
	assert.equal(billingBanner(_notice("grace"), true).type, "warning");
});

test("billingBanner: says nothing when this audience has no copy", () => {
	assert.equal(billingBanner({ phase: "grace", admin_message: "x" }, false), null);
});

test("billingBanner: retrying is an amber, persistent dunning banner", () => {
	// Slice 2: a failed auto-renewal in the gateway's retry window.
	const b = billingBanner(_notice("retrying"), true);
	assert.equal(b.type, "warning");
	assert.equal(b.title, "Payment retrying");
	assert.equal(b.message, "ADMIN COPY");
	assert.equal(b.dismissible, false); // persistent - only the pre-expiry nudge dismisses
	assert.equal(billingBanner(_notice("retrying"), false).message, "MEMBER COPY");
});
