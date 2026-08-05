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
