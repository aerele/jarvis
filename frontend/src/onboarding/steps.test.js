import { test } from "node:test";
import assert from "node:assert/strict";
import {
	STEPS_MANAGED,
	nextStep,
	prevStep,
	stepIndex,
	isOnboardComplete,
	suspensionNotice,
	SUSPENDED_FALLBACK,
	notReadyNote,
	GENERIC_NOT_READY_NOTE,
	syncStatusNote,
	planDueToday,
	planPricing,
	planSubtitleFor,
} from "./steps.js";

test("managed step order", () => {
	assert.deepEqual(STEPS_MANAGED, ["intro", "details", "plan", "pay", "connect"]);
	assert.equal(nextStep(STEPS_MANAGED, "plan"), "pay");
	assert.equal(prevStep(STEPS_MANAGED, "details"), "intro");
	assert.equal(nextStep(STEPS_MANAGED, "connect"), "connect"); // clamp at end
	// Off-funnel steps (e.g. "reconnect", which is deliberately absent so the rail
	// hides) clamp to index 0, so relative navigation from one silently walks the
	// customer back to the FIRST step. Anything off-funnel must set state.step
	// explicitly instead of calling goNext()/goBack().
	assert.equal(stepIndex(STEPS_MANAGED, "reconnect"), 0);
	assert.equal(nextStep(STEPS_MANAGED, "reconnect"), "details");
	// ...which is why finishing a reconnect re-anchors to "pay" first: that is where
	// the provisioning screen renders, and it is the step that advances to the end of
	// onboarding rather than back into the funnel.
	assert.equal(nextStep(STEPS_MANAGED, "pay"), "connect");
	assert.equal(prevStep(STEPS_MANAGED, "intro"), "intro"); // clamp at start
});

test("stepIndex", () => {
	assert.equal(stepIndex(STEPS_MANAGED, "pay"), 3);
});

// jarvis#536: the plan step's subtitle used to hardcode "Start free" even
// when nothing on offer was free or trial-gated. planSubtitleFor derives it
// from the plans actually fetched instead.
test("planSubtitleFor: no free or trial plan drops the free-tier promise", () => {
	assert.equal(
		planSubtitleFor([
			{ name: "ob-plan", price_inr: 1000, trial_days: 0 },
			{ name: "Pro (Annual)", price_inr: 3000, trial_days: 0 },
		]),
		"Pick a plan to get started. Billing continues automatically until you cancel."
	);
});

// A plan row whose price is absent, null or blank is NOT free. Number(null)
// is 0, so the naive coercion reported the free-tier wording for a catalog
// that charges for everything, which is the bug jarvis#536 was filed about.
test("planSubtitleFor: an absent, null or blank price is not treated as free", () => {
	const paidOnly =
		"Pick a plan to get started. Billing continues automatically until you cancel.";
	assert.equal(planSubtitleFor([{ name: "ob-plan", price_inr: null }]), paidOnly);
	assert.equal(planSubtitleFor([{ name: "ob-plan" }]), paidOnly);
	assert.equal(planSubtitleFor([{ name: "ob-plan", price_inr: "" }]), paidOnly);
	assert.equal(
		planSubtitleFor([{ name: "ob-plan", price_inr: 1000, trial_days: null }]),
		paidOnly
	);
	// A stray null element from a partially loaded response must not count either.
	assert.equal(planSubtitleFor([null, { name: "Pro", price_inr: 3000 }]), paidOnly);
	// A genuine zero still reads as free, including the string form.
	assert.equal(
		planSubtitleFor([{ name: "Free", price_inr: "0" }]),
		"Start free. Upgrade whenever you're ready."
	);
});

test("planSubtitleFor: a zero-price plan keeps the free-tier wording", () => {
	assert.equal(
		planSubtitleFor([
			{ name: "Free", price_inr: 0, trial_days: 0 },
			{ name: "Pro (Annual)", price_inr: 3000, trial_days: 0 },
		]),
		"Start free. Upgrade whenever you're ready."
	);
});

test("planSubtitleFor: a trial-gated plan does NOT promise that nothing renews", () => {
	assert.equal(
		planSubtitleFor([{ name: "Pro", price_inr: 3000, trial_days: 7 }]),
		"Start with a free trial. When it ends, billing continues automatically until you cancel."
	);
	// A genuinely free plan still may, because nothing renews on a zero price.
	assert.equal(
		planSubtitleFor([
			{ name: "Free", price_inr: 0 },
			{ name: "Pro", trial_days: 7 },
		]),
		"Start free. Upgrade whenever you're ready."
	);
});

test("planSubtitleFor: an empty or missing plan list is not a false promise", () => {
	assert.equal(
		planSubtitleFor([]),
		"Pick a plan to get started. Billing continues automatically until you cancel."
	);
	assert.equal(
		planSubtitleFor(null),
		"Pick a plan to get started. Billing continues automatically until you cancel."
	);
	assert.equal(
		planSubtitleFor(undefined),
		"Pick a plan to get started. Billing continues automatically until you cancel."
	);
});

// jarvis.account.is_ready_for_chat returns {ready: bool, reason: str|None}
// (see jarvis/account.py::is_ready_for_chat) - isOnboardComplete reads the
// real `ready` field, so this doubles as a real-shape regression test.
test("isOnboardComplete reads readiness", () => {
	assert.equal(isOnboardComplete({ ready: true, reason: null }), true);
	assert.equal(isOnboardComplete({ ready: false, reason: "signup" }), false);
	assert.equal(isOnboardComplete({ ready: false, reason: "llm_credentials" }), false);
	assert.equal(isOnboardComplete(null), false);
	assert.equal(isOnboardComplete(undefined), false);
});

// --------------------------------------------------------------------------- //
// GST tax breakdown (2026-08) - base/GST/total split, canonical rounding
// --------------------------------------------------------------------------- //
test("planPricing splits base + GST with canonical rounding", () => {
	assert.deepEqual(
		planPricing({
			plan_name: "Standard",
			price_inr: 3500,
			gst_percent: 18,
			billing_cycle: "Monthly",
		}),
		{ subtotal: 3500, gstPercent: 18, gstAmount: 630, total: 4130 }
	);
});

test("planPricing: no gst_percent (absent or zero) collapses to identity", () => {
	assert.deepEqual(planPricing({ price_inr: 3500 }), {
		subtotal: 3500,
		gstPercent: 0,
		gstAmount: 0,
		total: 3500,
	});
	assert.deepEqual(planPricing({ price_inr: 3500, gst_percent: 0 }), {
		subtotal: 3500,
		gstPercent: 0,
		gstAmount: 0,
		total: 3500,
	});
});

test("planPricing: a missing plan is a zeroed split, not a crash", () => {
	assert.deepEqual(planPricing(null), { subtotal: 0, gstPercent: 0, gstAmount: 0, total: 0 });
});

// price_inr=3475, gst_percent=18 -> gstAmount=625.5, total=4100.5: a genuine
// paise-precision result (gstAmount is NOT a whole rupee). The backend charges
// this exact value (to_paise -> 410050), so the split - and everything
// downstream of it - must carry the fraction rather than rounding it away.
test("planPricing: a fractional GST result keeps its paise precision", () => {
	assert.deepEqual(
		planPricing({
			plan_name: "Standard",
			price_inr: 3475,
			gst_percent: 18,
			billing_cycle: "Monthly",
		}),
		{ subtotal: 3475, gstPercent: 18, gstAmount: 625.5, total: 4100.5 }
	);
});

// --------------------------------------------------------------------------- //
// #671 - what is due TODAY, shown on the Plan card and the Review row
// --------------------------------------------------------------------------- //
test("planDueToday: a trial plan leads with zero due now, then the tax-inclusive recurring price", () => {
	assert.equal(
		planDueToday({
			plan_name: "Standard",
			price_inr: 3500,
			gst_percent: 18,
			billing_cycle: "Monthly",
			trial_days: 7,
		}),
		"₹0 today · then ₹4,130/mo after 7 days"
	);
});

test("planDueToday: a signup fee is what is due now, not zero (fee itself is untaxed)", () => {
	// admin's get_plans comment: pricing from price_inr alone understates the charge,
	// "most visibly on a trial plan, where the fee is the entire amount due now". The
	// Review row this replaces hardcoded a flat zero for every trial. The signup fee is
	// NOT taxed in this scope - GST applies to the recurring plan price only.
	assert.equal(
		planDueToday({
			plan_name: "Standard",
			price_inr: 3500,
			gst_percent: 18,
			billing_cycle: "Monthly",
			trial_days: 7,
			signup_fee_inr: 499,
		}),
		"₹499 today · then ₹4,130/mo after 7 days"
	);
});

test("planDueToday: a plan with no trial and no gst_percent just shows its pre-tax price", () => {
	// Backward-compat: a plan that never carries gst_percent (or carries 0) behaves
	// exactly as before GST existed. inr(total) collapses to inr(subtotal) here.
	// The card already renders the big price with its own "/mo" directly above this
	// line, so no cycle suffix is repeated here either.
	assert.equal(
		planDueToday({ plan_name: "Pro", price_inr: 3500, billing_cycle: "Monthly" }),
		"₹3,500"
	);
});

test("planDueToday: a no-trial plan shows the tax-inclusive price when gst_percent is present", () => {
	assert.equal(
		planDueToday({
			plan_name: "Pro",
			price_inr: 3500,
			gst_percent: 18,
			billing_cycle: "Monthly",
		}),
		"₹4,130"
	);
});

test("planDueToday: a no-trial plan with a fee shows the combined tax-inclusive amount", () => {
	assert.equal(
		planDueToday({
			plan_name: "Pro",
			price_inr: 3500,
			gst_percent: 18,
			billing_cycle: "Monthly",
			signup_fee_inr: 500,
		}),
		"₹4,630 today"
	);
});

test("planDueToday: nothing to say without a plan", () => {
	assert.equal(planDueToday(null), "");
	assert.equal(planDueToday({}), "");
});

// A fractional total (price_inr=3475, gst_percent=18 -> total=4100.5) must
// display the EXACT charged value, not inr()'s bare one-decimal "₹4,100.5"
// nor a rounded-off "₹4,101" - either would diverge from the paise-precise
// amount the backend actually charges (to_paise -> 410050).
test("planDueToday: a fractional GST total renders its exact paise-precise value, not rounded", () => {
	assert.equal(
		planDueToday({
			plan_name: "Standard",
			price_inr: 3475,
			gst_percent: 18,
			billing_cycle: "Monthly",
		}),
		"₹4,100.50"
	);
});

test("planDueToday: a fractional GST total on a trial plan renders exactly in the 'then' clause too", () => {
	assert.equal(
		planDueToday({
			plan_name: "Standard",
			price_inr: 3475,
			gst_percent: 18,
			billing_cycle: "Monthly",
			trial_days: 7,
		}),
		"₹0 today · then ₹4,100.50/mo after 7 days"
	);
});
