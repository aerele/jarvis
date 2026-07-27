// Pure step-progression helpers for the onboarding wizard. No Vue, no API
// calls - kept pure so they're cheap to unit-test with node --test (see
// steps.test.js) and reusable from both the wizard component and the
// router's first-run guard.

// Managed flow (2026-07 redesign): intro tour → Plan → Details → Pay → Connect.
// The old "mode" chooser + "account" step are gone; self-host is reached via a
// quiet link on the Plan step and keeps its single "selfhost" step.
export const STEPS_MANAGED = ["intro", "plan", "details", "pay", "connect"];
export const STEPS_SELFHOST = ["plan", "selfhost"];

export function stepIndex(steps, cur) {
	const i = steps.indexOf(cur);
	return i < 0 ? 0 : i;
}

export function nextStep(steps, cur) {
	return steps[Math.min(stepIndex(steps, cur) + 1, steps.length - 1)];
}

export function prevStep(steps, cur) {
	return steps[Math.max(stepIndex(steps, cur) - 1, 0)];
}

// jarvis.account.is_ready_for_chat (jarvis/account.py) returns
// {ready: bool, reason: str|None} - reason is one of "signup" /
// "llm_credentials" / "selfhost_connection" when not ready, null when ready.
// Onboarding is "complete" (chat-ready) exactly when `ready` is true.
export function isOnboardComplete(readyResp) {
	return !!(readyResp && readyResp.ready);
}

// Used when an older admin sends the Suspended state without a reason string.
export const SUSPENDED_FALLBACK =
	"Your subscription is no longer active. Renew to restore access to Jarvis.";

// The renew-banner sentence, or the admin's own explanation for a stalled
// container_provisioning workspace - null when neither reason applies (a
// different not-ready reason, ready:true, or an absent response). Kept out
// of NOT_ONBOARDED_REASONS on purpose: the workspace is set up, not
// un-onboarded, so it renders normally with a banner rather than the setup
// poster.
//
// container_provisioning used to return null here unconditionally - one of
// the places the 2026-07-23 out-of-quota trace found the real reason
// (already computed by _admin_chat_gate and carried in `detail`) getting
// dropped on the floor before it ever reached the customer. Unlike
// subscription_suspended there is no single fallback sentence that fits
// every container_provisioning cause (a provisioning stall and an
// out-of-quota LLM account need different copy), so a MISSING detail here
// still returns null - callers own their own generic fallback (see
// notReadyNote below, and readiness.js's readinessDetailOf).
export function suspensionNotice(readyResp) {
	if (!readyResp || readyResp.ready) return null;
	if (readyResp.reason === "subscription_suspended") {
		return readyResp.detail || SUSPENDED_FALLBACK;
	}
	if (readyResp.reason === "container_provisioning") {
		return readyResp.detail || null;
	}
	return null;
}

// The onboarding "still not ready" banner's copy (Connect + self-host post-save
// recheck, OnboardingView's afterSaveRecheckReady): prefer the backend's OWN
// sentence (jarvis.account.is_ready_for_chat's `detail`) over a generic shrug.
// `detail` can be missing (an older admin, or a reason account.py has no wording
// for yet) or blank; only then does the generic copy apply - it must never
// overwrite a real explanation such as "Your OpenAI account has reached its
// usage limit. It resets in about 27 hours."
//
// Whitelabelling: develop moved the wizard's copy onto `agentName` (@/branding,
// window.agent_name with a "Jarvis" default). This module stays PURE on purpose
// - node --test runs it with no `window` and no `@` alias - so the agent name
// arrives as an argument instead of an import, and OnboardingView passes its
// already-imported `agentName`. The default keeps every existing caller and the
// exported constant byte-identical for a non-whitelabeled tenant.
export const DEFAULT_AGENT_NAME = "Jarvis";

export function genericNotReadyNote(agent = DEFAULT_AGENT_NAME) {
	return `Still finishing setup. This can take a few seconds. You can continue to ${agent} now, or wait and try again.`;
}
export const GENERIC_NOT_READY_NOTE = genericNotReadyNote();
export function notReadyNote(detail, agent = DEFAULT_AGENT_NAME) {
	const d = (detail || "").trim();
	return d || genericNotReadyNote(agent);
}

// A terminal `last_sync_status` ("failed: ..." / "skipped: ...") reason ALWAYS
// reads as a real customer sentence when it matches admin's own second-person
// convention: jarvis_settings.py's `_admin_customer_facing_reason` (and, one
// layer further up, jarvis_admin_v2.fleet.pool's `_pool_route_reason` /
// `_quota_exhausted_sentence`) ONLY ever write prose that starts with "Your "
// and ends with "." (e.g. "Your OpenAI account has reached its usage limit.
// It resets in about 27 hours."). Every other terminal status this field can
// hold - "failed: auth: ...", "failed: rate-limited; retry shortly",
// "failed: unexpected error; see Error Log", "skipped: no longer proxy-valid
// after re-read (...)" - is developer/diagnostic text and stays wrapped in
// the generic explanatory copy below. Mirror any change to the "Your ... ."
// shape check on the admin/bench side in this same regex.
const SYNC_STATUS_REASON_RE = /^(?:failed|skipped):\s*(.*)$/s;

export function syncStatusNote(status, agent = DEFAULT_AGENT_NAME) {
	const s = (status || "").trim();
	const m = SYNC_STATUS_REASON_RE.exec(s);
	const reason = (m ? m[1] : "").trim();
	if (reason.startsWith("Your ") && reason.endsWith(".")) {
		return reason;
	}
	return `Setup hit a problem (${s}). Check the AI connection and save again - or continue to ${agent} and retry from Settings.`;
}

// Branch decision for the "I've verified my email" poll. Admin's
// get_signup_payment_state (jarvis_admin_v2/billing/signup.py) returns one of
// THREE shapes: still-pending, paid-plan order handles, or the free/trial
// completion {pending_verification: false, subscription_status: "..."} with
// no order (verification WAS the whole signup). Pure so the free-plan branch
// - the one that used to dead-end on "Signup state has changed" - stays
// unit-tested.
//   {kind: "wait"}                          - link not clicked yet (or empty resp)
//   {kind: "checkout", provider: "..."}     - paid plan: open the provider's Checkout
//   {kind: "complete"}                      - free/trial plan: already Active, skip
//                                             payment and go straight to provisioning
//   {kind: "halted", status: "..."}         - Cancelled/Expired/etc: dead sub, tell
//                                             the customer instead of the generic
//                                             "state changed" shrug
//   {kind: "stale"}                         - unrecognized shape: ask for a refresh
export function verifyPollAction(d) {
	if (!d || d.pending_verification) return { kind: "wait" };
	// checkout covers both gateways in both shapes: Razorpay (one-shot order
	// `razorpay_order_id`, autopay mandate `razorpay_subscription_id`) and
	// Cashfree (one-shot order `payment_session_id`, autopay mandate
	// `subscription_session_id`). Missing the mandate token here would strand a
	// resuming Cashfree autopay customer on "stale" with no way to pay.
	// The provider rides the response so the caller launches the right SDK;
	// absent ⇒ razorpay (an older admin that predates the discriminator).
	const prov = (d.payment_provider || d.provider || "").toLowerCase();
	const hasRz = d.razorpay_order_id || d.razorpay_subscription_id;
	const hasCf = d.payment_session_id || d.subscription_session_id;
	if (hasRz || hasCf) {
		return { kind: "checkout", provider: prov || (hasCf ? "cashfree" : "razorpay") };
	}
	if (d.subscription_status === "Active") return { kind: "complete" };
	if (d.subscription_status) return { kind: "halted", status: String(d.subscription_status) };
	return { kind: "stale" };
}
