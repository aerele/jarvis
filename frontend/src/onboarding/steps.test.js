import { test } from "node:test";
import assert from "node:assert/strict";
import {
	STEPS_MANAGED,
	STEPS_SELFHOST,
	nextStep,
	prevStep,
	stepIndex,
	isOnboardComplete,
	verifyPollAction,
	suspensionNotice,
	SUSPENDED_FALLBACK,
	notReadyNote,
	GENERIC_NOT_READY_NOTE,
	syncStatusNote,
} from "./steps.js";

test("managed step order", () => {
	assert.deepEqual(STEPS_MANAGED, ["intro", "plan", "details", "pay", "connect"]);
	assert.equal(nextStep(STEPS_MANAGED, "plan"), "details");
	assert.equal(prevStep(STEPS_MANAGED, "details"), "plan");
	assert.equal(nextStep(STEPS_MANAGED, "connect"), "connect"); // clamp at end
	assert.equal(prevStep(STEPS_MANAGED, "intro"), "intro"); // clamp at start
});

test("selfhost step order", () => {
	assert.deepEqual(STEPS_SELFHOST, ["plan", "selfhost"]);
	assert.equal(nextStep(STEPS_SELFHOST, "plan"), "selfhost");
	assert.equal(prevStep(STEPS_SELFHOST, "selfhost"), "plan");
});

test("stepIndex", () => {
	assert.equal(stepIndex(STEPS_MANAGED, "pay"), 3);
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

// verifyPollAction consumes admin's get_signup_payment_state shapes verbatim
// (jarvis_admin_v2/billing/signup.py::get_signup_payment_state docstring).
// The "complete" case is the free-plan fix: {pending_verification: false,
// subscription_status: "Active"} with NO razorpay_order_id used to fall
// through every branch and dead-end the wizard.
test("verifyPollAction: link not clicked yet keeps waiting", () => {
	assert.deepEqual(verifyPollAction({ pending_verification: true }), { kind: "wait" });
	assert.deepEqual(verifyPollAction(null), { kind: "wait" });
	assert.deepEqual(verifyPollAction(undefined), { kind: "wait" });
});

test("verifyPollAction: paid plan goes to checkout", () => {
	const d = {
		pending_verification: false,
		razorpay_order_id: "order_x",
		razorpay_key_id: "rzp_test",
		amount_inr: 2000,
		customer_password: "pw",
	};
	assert.deepEqual(verifyPollAction(d), { kind: "checkout", provider: "razorpay" });
});

test("verifyPollAction: autopay-trial (subscription) plan goes to checkout", () => {
	// Admin's poll shape for a paid plan with trial_days: a Razorpay
	// SUBSCRIPTION id (mandate-auth Checkout), no order id.
	const d = {
		pending_verification: false,
		razorpay_subscription_id: "sub_x",
		razorpay_key_id: "rzp_test",
		amount_inr: 999,
		trial_days: 14,
		customer_password: "pw",
	};
	assert.deepEqual(verifyPollAction(d), { kind: "checkout", provider: "razorpay" });
});

test("verifyPollAction: cashfree paid plan goes to checkout (provider labeled)", () => {
	// Admin's poll shape for a Cashfree paid plan: a payment_session_id (the
	// token the Cashfree SDK checks out against) + the provider discriminator.
	const d = {
		pending_verification: false,
		payment_provider: "cashfree",
		cashfree_order_id: "cforder_x",
		payment_session_id: "session_x",
		cashfree_app_id: "app_x",
		cashfree_env: "sandbox",
		amount_inr: 2000,
	};
	assert.deepEqual(verifyPollAction(d), { kind: "checkout", provider: "cashfree" });
});

// Autopay monthly on Cashfree resumes with a MANDATE token, not an order one.
// Before this was recognised the customer landed on "stale" and could not pay.
test("verifyPollAction: cashfree autopay mandate goes to checkout", () => {
	const d = {
		pending_verification: false,
		payment_provider: "cashfree",
		cashfree_subscription_id: "jrvs_x",
		subscription_session_id: "sub_session_x",
		cashfree_app_id: "app_x",
		cashfree_env: "sandbox",
	};
	assert.deepEqual(verifyPollAction(d), { kind: "checkout", provider: "cashfree" });
});

test("verifyPollAction: cashfree mandate inferred from subscription_session_id alone", () => {
	const d = { pending_verification: false, subscription_session_id: "sub_session_x" };
	assert.deepEqual(verifyPollAction(d), { kind: "checkout", provider: "cashfree" });
});

test("verifyPollAction: cashfree inferred from payment_session_id when provider absent", () => {
	const d = { pending_verification: false, payment_session_id: "session_y", amount_inr: 500 };
	assert.deepEqual(verifyPollAction(d), { kind: "checkout", provider: "cashfree" });
});

test("verifyPollAction: free plan already Active completes without payment", () => {
	const d = {
		pending_verification: false,
		subscription_status: "Active",
		customer_password: "pw",
	};
	assert.deepEqual(verifyPollAction(d), { kind: "complete" });
});

test("verifyPollAction: terminal statuses surface as halted", () => {
	for (const status of ["Cancelled", "Expired", "Past Due"]) {
		assert.deepEqual(
			verifyPollAction({ pending_verification: false, subscription_status: status }),
			{ kind: "halted", status },
		);
	}
});

test("verifyPollAction: unknown shape asks for a refresh", () => {
	assert.deepEqual(verifyPollAction({ pending_verification: false }), { kind: "stale" });
	assert.deepEqual(verifyPollAction({}), { kind: "stale" });
});

test("suspensionNotice: only fires for a suspended or provisioning-blocked workspace", () => {
	// Entitled / ready / absent → no banner.
	assert.equal(suspensionNotice({ ready: true }), null);
	assert.equal(suspensionNotice(null), null);
	assert.equal(suspensionNotice(undefined), null);
	// A degraded-but-not-blocking reason must NOT raise this banner - llm_credentials
	// stays on the existing invite/reauthorize path, not this one.
	assert.equal(suspensionNotice({ ready: false, reason: "llm_credentials" }), null);
	// container_provisioning WITH NO detail also returns null here - this is the
	// 2026-07-23 trace's regression pin, updated deliberately (see the next test for
	// the case that changed): a v1/older admin that never sends `chat_readiness_reason`
	// must not make this function invent a sentence it was never given. It is NOT
	// filtered out by reason anymore (see below) - it is null purely because there is
	// no detail to show, same as any other "nothing to say yet" case.
	assert.equal(suspensionNotice({ ready: false, reason: "container_provisioning" }), null);
});

test("suspensionNotice: container_provisioning surfaces admin's detail too", () => {
	// Before this, container_provisioning was hardcoded to null regardless of detail -
	// exactly how the out-of-quota trace's real sentence ("Your OpenAI account has
	// reached its usage limit...") got dropped before it ever reached the customer.
	// Unlike subscription_suspended there is no generic fallback sentence for this
	// reason (a provisioning stall and an out-of-quota account need different copy),
	// so an ABSENT detail still returns null (covered above) rather than a made-up one.
	assert.equal(
		suspensionNotice({
			ready: false,
			reason: "container_provisioning",
			detail: "Your OpenAI account has reached its usage limit. It resets in about 27 hours.",
		}),
		"Your OpenAI account has reached its usage limit. It resets in about 27 hours."
	);
});

test("suspensionNotice: prefers admin's sentence, falls back when absent", () => {
	assert.equal(
		suspensionNotice({
			ready: false,
			reason: "subscription_suspended",
			detail: "Your subscription has expired. Renew to restore access to Jarvis.",
		}),
		"Your subscription has expired. Renew to restore access to Jarvis.",
	);
	// Older admin sends the state with no reason string.
	assert.equal(
		suspensionNotice({ ready: false, reason: "subscription_suspended" }),
		SUSPENDED_FALLBACK,
	);
	assert.equal(
		suspensionNotice({ ready: false, reason: "subscription_suspended", detail: "" }),
		SUSPENDED_FALLBACK,
	);
});

// ---- notReadyNote: the onboarding "still not ready" banner's copy ----------------
// Before this, OnboardingView hardcoded a single generic sentence regardless of what
// account.py's is_ready_for_chat actually knew - the exact "every layer threw the
// reason away" bug this whole fix addresses.
test("notReadyNote: prefers the backend's real detail", () => {
	assert.equal(
		notReadyNote(
			"Your OpenAI account has reached its usage limit. It resets in about 27 hours."
		),
		"Your OpenAI account has reached its usage limit. It resets in about 27 hours."
	);
});

test("notReadyNote: falls back to the generic copy only when detail is genuinely absent", () => {
	assert.equal(notReadyNote(""), GENERIC_NOT_READY_NOTE);
	assert.equal(notReadyNote(null), GENERIC_NOT_READY_NOTE);
	assert.equal(notReadyNote(undefined), GENERIC_NOT_READY_NOTE);
	assert.equal(notReadyNote("   "), GENERIC_NOT_READY_NOTE); // whitespace-only
});

// Whitelabelling (merge with develop): the wizard renders `agentName`, so the
// fallback copy that moved into this module must not hardcode "Jarvis". These
// helpers stay pure - the name arrives as an argument, never as a window read.
test("notReadyNote: the generic fallback is whitelabelled, the real detail is untouched", () => {
	assert.match(notReadyNote("", "Aerele Bot"), /continue to Aerele Bot now/);
	assert.equal(notReadyNote("", "Jarvis"), GENERIC_NOT_READY_NOTE);
	// A real backend sentence is the admin's own wording and must never have a
	// tenant's agent name spliced into it.
	assert.equal(
		notReadyNote("Your OpenAI account has reached its usage limit.", "Aerele Bot"),
		"Your OpenAI account has reached its usage limit."
	);
});

test("syncStatusNote: the opaque-status wrapper is whitelabelled", () => {
	assert.match(
		syncStatusNote("failed: unexpected error; see Error Log", "Aerele Bot"),
		/continue to Aerele Bot and retry from Settings/
	);
	// An unwrapped real sentence stays byte-identical regardless of agent name.
	assert.equal(
		syncStatusNote("failed: Your OpenAI account has reached its usage limit.", "Aerele Bot"),
		"Your OpenAI account has reached its usage limit."
	);
});

// ---- syncStatusNote: the "Setup hit a problem (...)" banner's copy --------------
// Before this fix, EVERY "failed:"/"skipped:" last_sync_status was wrapped verbatim
// in "Setup hit a problem (${status})..." - so a real reason (jarvis_settings.py now
// writes "failed: Your OpenAI account has reached its usage limit...") read as buried
// developer text instead of a sentence a customer could act on (2026-07-23 trace).

test("syncStatusNote: renders a real customer sentence directly, unwrapped", () => {
	assert.equal(
		syncStatusNote(
			"failed: Your OpenAI account has reached its usage limit. It resets in about 27 hours."
		),
		"Your OpenAI account has reached its usage limit. It resets in about 27 hours."
	);
});

test("syncStatusNote: a skipped status with a real sentence is also unwrapped", () => {
	assert.equal(
		syncStatusNote(
			"skipped: Your Google Gemini subscription was rejected. Reconnect the account."
		),
		"Your Google Gemini subscription was rejected. Reconnect the account."
	);
});

test("syncStatusNote: keeps the generic wrapper for genuinely opaque statuses", () => {
	assert.equal(
		syncStatusNote("failed: unexpected error; see Error Log"),
		"Setup hit a problem (failed: unexpected error; see Error Log). Check the AI connection and " +
			"save again - or continue to Jarvis and retry from Settings."
	);
	assert.equal(
		syncStatusNote("failed: auth: invalid token"),
		"Setup hit a problem (failed: auth: invalid token). Check the AI connection and save again - " +
			"or continue to Jarvis and retry from Settings."
	);
	assert.equal(
		syncStatusNote("failed: rate-limited; retry shortly"),
		"Setup hit a problem (failed: rate-limited; retry shortly). Check the AI connection and save " +
			"again - or continue to Jarvis and retry from Settings."
	);
	assert.equal(
		syncStatusNote("skipped: no longer proxy-valid after re-read (some reason)"),
		"Setup hit a problem (skipped: no longer proxy-valid after re-read (some reason)). Check the " +
			"AI connection and save again - or continue to Jarvis and retry from Settings."
	);
});

test("syncStatusNote: a bare reason with no failed/skipped prefix stays wrapped", () => {
	// syncStatusNote is only ever called after the caller has already checked
	// status.startsWith("failed"/"skipped"); this pins that an unprefixed string
	// (defensive - should not happen in practice) degrades to the wrapper rather
	// than being misread as a bare sentence.
	assert.equal(
		syncStatusNote("Your OpenAI account has reached its usage limit."),
		"Setup hit a problem (Your OpenAI account has reached its usage limit.). Check the AI " +
			"connection and save again - or continue to Jarvis and retry from Settings."
	);
});
