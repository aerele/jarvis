// Pure step-progression helpers for the onboarding wizard. No Vue, no API
// calls - kept pure so they're cheap to unit-test with node --test (see
// steps.test.js) and reusable from both the wizard component and the
// router's first-run guard.

// Managed flow (2026-07 redesign): intro tour → Plan → Details → Pay → Connect.
// The old "mode" chooser + "account" step are gone.
// Details BEFORE plan: the email + company are what identify an account, so asking
// for them first is what lets a returning customer take the reconnect exit before
// weighing plans they already pay for. Nothing on the Details step reads the chosen
// plan, so the two are free to swap.
export const STEPS_MANAGED = ["intro", "details", "plan", "pay", "connect"];

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

// Plan step subtitle (OnboardingView's "Choose your plan" header): "Start
// free" is only honest when the catalog actually has something free or
// trial-gated on offer. The admin plan catalog can deactivate the Free plan
// on its own (jarvis#536), at which point every selectable plan charges
// immediately, so the copy has to track the plans that were actually
// fetched rather than being hardcoded. Pure so the no-free-plan branch
// stays unit-tested without mounting the wizard.
export function planSubtitleFor(plans) {
	const list = Array.isArray(plans) ? plans : [];
	// A missing price must NOT read as free: Number(null) is 0, so a plan row
	// that simply never carried price_inr would otherwise resurrect the exact
	// false promise this function exists to remove. Absent, null and blank all
	// coerce to NaN here, which fails both comparisons, so the honest paid-only
	// wording wins whenever the catalog is not explicit about being free.
	const amount = (v) => (v === null || v === undefined || v === "" ? NaN : Number(v));
	const hasFreeOrTrial = list.some(
		(p) => !!p && (amount(p.price_inr) === 0 || amount(p.trial_days) > 0)
	);
	return hasFreeOrTrial
		? "Start free. Upgrade or extend anytime, with no auto-renewal."
		: "Pick a plan to get started. No auto-renewal.";
}

// jarvis.account.is_ready_for_chat (jarvis/account.py) returns
// {ready: bool, reason: str|None} - reason is one of "signup" /
// "llm_credentials" and friends when not ready, null when ready.
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

// The onboarding "still not ready" banner's copy (the Connect post-save
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
// "failed: unexpected error; see Error Log", "skipped: no longer pool-valid
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
