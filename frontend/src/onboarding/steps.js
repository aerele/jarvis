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

// RELATIVE, not "@/account/format": this file's tests run under `node --test`
// (package.json test:node), which resolves no bundler alias. format.js is plain ESM
// with zero imports of its own, so pulling it in here keeps this module cheap to test.
import { inr, planSuffix } from "../account/format.js";

/**
 * Base/GST/total split for one plan (GST tax breakdown, 2026-08).
 *
 * CANONICAL formula, identical to the backend's `jarvis_admin_v2.billing.catalog`
 * (`gst_amount_inr` / `total_inr`): `gstAmount = round(subtotal * gstPercent / 100, 2)`,
 * `total = subtotal + gstAmount`. Rounding order must match the backend exactly - the
 * gateway order is created at `total` and later verified against it, so a drift here
 * would reject a real payment as an amount mismatch.
 *
 * `price_inr` is always the pre-tax base (its own field description says so); a plan
 * with no `gst_percent` (or 0) collapses to `total === subtotal`, so callers written
 * before GST existed keep behaving exactly as before.
 */
export function planPricing(p) {
	const plan = p || {};
	const subtotal = Number(plan.price_inr) || 0;
	const gstPercent = Number(plan.gst_percent) || 0;
	const gstAmount = Math.round(subtotal * gstPercent) / 100; // round(base*pct/100, 2)
	return { subtotal, gstPercent, gstAmount, total: subtotal + gstAmount };
}

/**
 * What the customer pays TODAY for one plan, as a sentence (#671).
 *
 * ONE definition, used by the Plan cards and the Review row. The customer used to
 * learn this only on Review, a screen after choosing, so nothing told them the trial
 * starts at zero while they were still deciding. Two independent renderings of "what
 * you pay now" is also how the two screens come to disagree.
 *
 * ``signup_fee_inr`` is included. admin's own get_plans comment says pricing from
 * ``price_inr`` alone understates the charge, "most visibly on a trial plan, where the
 * fee is the entire amount due now", and the Review row this replaces hardcoded a flat
 * zero for every trial, which is exactly that understatement. The fee is 0 on the
 * current catalog, so this changes nothing today and stops being wrong the moment a
 * plan carries one.
 *
 * The recurring/charged amount is `planPricing(p).total` (tax-inclusive) - the fee
 * itself is NOT taxed in this scope (GST applies to the recurring plan price only).
 */
export function planDueToday(p) {
	if (!p || !p.plan_name) return "";
	const fee = Number(p.signup_fee_inr) || 0;
	const days = Number(p.trial_days) || 0;
	const { total } = planPricing(p);
	const suffix = planSuffix(p.price_inr, p.billing_cycle) || "";
	if (days > 0) {
		return `${inr(fee)} today · then ${inr(total)}${suffix} after ${days} days`;
	}
	return fee > 0 ? `${inr(total + fee)} today` : inr(total);
}

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
	// A genuinely FREE plan (price zero) and a TRIAL plan are not the same promise,
	// and collapsing them is how this line came to say the opposite of what happens.
	// A trial is gated behind a Razorpay RECURRING MANDATE: the card is rejected
	// unless it is recurring-eligible, and when the trial ends the plan bills
	// monthly until cancelled. So "no auto-renewal" was, for a trial-only catalog,
	// billing consent copy that contradicted the instrument being authorized three
	// screens later. Only a zero-price plan can honestly claim nothing renews.
	const hasFree = list.some((p) => !!p && amount(p.price_inr) === 0);
	const hasTrial = list.some((p) => !!p && amount(p.trial_days) > 0);
	if (hasFree) return "Start free. Upgrade whenever you're ready.";
	if (hasTrial) {
		return "Start with a free trial. When it ends, billing continues automatically until you cancel.";
	}
	return "Pick a plan to get started. Billing continues automatically until you cancel.";
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
