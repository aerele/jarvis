import { isReadyForChat } from "@/api.js";
import { isOnboardComplete } from "@/onboarding/steps.js";

// Shared, memoized readiness verdict. Two callers need it per page load: the
// router's first-navigation guard (bounce an already-onboarded user off a stale
// /onboarding link) and the AppShell onboarding gate (block the app with a
// poster when the workspace was NEVER set up). Sharing one in-flight promise
// keeps it to a SINGLE backend round-trip.
//
// OnboardingView hard-reloads to /jarvis/ on completion, which re-mounts the SPA
// and drops this cache; that covers the wizard. It does NOT cover changes made
// in-app, which is why forgetReady() below exists: connecting or disconnecting a
// model from the settings dialog changes this verdict without leaving the page.
let readyPromise = null;

// Fail-CLOSED-retryable: if the readiness call itself THROWS (the customer's own
// bench erroring, not admin - the server already resolves an admin outage by
// cohort and returns a real verdict), do NOT declare the workspace ready. The old
// thrown->{ready:true} shortcut sent a never-onboarded workspace straight into a
// chat that cannot answer (review P0-05). Return the same retryable verdict the
// server uses for "nobody could confirm this", which needsOnboarding() gates on:
// an established workspace's outage is already handled server-side, so a thrown
// call here is the severe case and must fail closed with a retry, not open.
export function checkReady() {
	if (!readyPromise) {
		readyPromise = isReadyForChat().catch(() => ({
			ready: false,
			reason: "readiness_unconfirmed",
			retryable: true,
		}));
	}
	return readyPromise;
}

// Drop the memoized verdict so the NEXT checkReady() asks the backend again.
// Deliberately not a re-fetch: the callers that care re-read straight afterwards,
// and a caller that does not must not pay for a round-trip it never reads.
export function forgetReady() {
	readyPromise = null;
}

// Resolves true once the workspace is chat-ready. Used by the router guard.
export async function isWorkspaceReady() {
	return isOnboardComplete(await checkReady());
}

// Reasons (from account.py:is_ready_for_chat) that mean "this workspace has
// never completed onboarding" — the FIRST setup step:
//   - "signup"  no admin api_key yet (wizard not started)
// Deliberately NOT "llm_credentials": that reason ALSO fires when an
// already-onboarded workspace's LLM creds later expire/rotate. Hard-blocking a
// working workspace out of its chat + data over a recoverable credential
// problem is wrong — that case stays on the existing invite/banner path and
// keeps /account reachable so an admin can reauthorize.
// "llm_pool_provisioning" also belongs here: a pool is configured but its
// FIRST apply never succeeded (llm_pool_synced_at never stamped) - the
// workspace has never had a working AI connection, so chat can only fail;
// the poster routes back to setup. Unlike llm_credentials this cannot fire
// on an established workspace: once a pool applies ONCE the marker is
// permanent.
// "llm_provisioning" is the DIRECT (single-model) analogue (round-4 review
// R4-P0-6): a direct config whose first apply admin never confirmed
// (llm_direct_synced_at never stamped). Same permanence guarantee — once a
// direct config confirms once, the marker is permanent — so it belongs here
// too, never on the degraded-banner path.
// "llm_setup" is the server-decided hard variant of llm_credentials: creds
// missing AND nothing ever synced AND the subscription never went Active — a
// half-finished signup (e.g. failed payment), not an established workspace.
// The soft/hard split lives server-side (_llm_missing_verdict) because only
// admin knows the subscription state.
// "readiness_unconfirmed" is COHORT-AWARE by construction: the server returns it
// ONLY for a workspace nothing has ever confirmed chat-ready (an established one
// gets ready:true through the same outage - account._admin_unreachable_verdict).
// So when the SPA sees it, it is the never-ready cohort and must gate closed with
// a retry, exactly the case the full-screen poster is for (review P0-05). The
// thrown-call catch above resolves to this same reason for the severe own-bench
// failure, which also fails closed. It differs from llm_credentials, which fires
// on an ESTABLISHED workspace's later credential rotation and must stay degraded.
const NOT_ONBOARDED_REASONS = new Set([
	"signup",
	"llm_pool_provisioning",
	"llm_provisioning",
	"llm_setup",
	"readiness_unconfirmed",
]);

// True only when the workspace has NOT completed onboarding at all — the single
// case the full-screen gate poster is for. A ready workspace or a merely-degraded
// one (llm_credentials) returns false; a never-ready workspace the server could
// not confirm (readiness_unconfirmed) returns true, and the poster offers a retry.
export async function needsOnboarding() {
	const resp = await checkReady();
	if (isOnboardComplete(resp)) return false;
	return NOT_ONBOARDED_REASONS.has(resp && resp.reason);
}

// Billing banner payload from the same memoized verdict - no extra round-trip.
// The account was reconnected on ANOTHER site, so this one lost the workspace.
// `site_replaced` is deliberately absent from NOT_ONBOARDED_REASONS: this site IS
// onboarded, it just no longer holds the account, and the full-screen setup poster
// would say the wrong thing. Returns {} unless it applies.
export async function replacedNoticeOf() {
	const r = await checkReady();
	if (!r || r.reason !== "site_replaced") return {};
	return r.replaced_notice || { replaced: true };
}

// Where that banner's action sends the customer, and how the wizard recognises it.
// One constant for both ends: without the flag the wizard resumes its own path,
// and a site whose key was rotated away dead-ends at "could not sign in".
export const RECONNECT_INTENT_URL = "/jarvis/onboarding?reconnect=1";

// Which step the wizard opens on. Explicit reconnect intent beats a resumed
// step, but never a paid/provisioning one - somebody already set up is not
// reconnecting.
export function landingStep({ intent, resumedStep, terminal }) {
	return intent && !terminal ? "details" : resumedStep;
}

export function hasReconnectIntent(search) {
	try {
		return new URLSearchParams(search || "").get("reconnect") === "1";
	} catch {
		return false;
	}
}

// Copy for that banner. `moved_to` is the site now holding the account - both
// belong to the same account holder, so naming it is what makes this actionable.
export function replacedBanner(notice) {
	if (!notice || !notice.replaced) return null;
	const where = (notice.moved_to || "").replace(/^https?:\/\//, "").replace(/\/$/, "");
	return {
		tone: "warning",
		title: "This site no longer has access to your workspace",
		message: where
			? `Your Jarvis account is now connected to ${where}. Chat here stopped working when it moved.`
			: "Your Jarvis account was reconnected on another site. Chat here stopped working when it moved.",
		action: "Reconnect this site instead",
	};
}

export async function billingNoticeOf() {
	const r = await checkReady();
	return (r && r.billing_notice) || {};
}

// The backend's OWN explanation for a "container_provisioning" not-ready verdict
// (jarvis.account.is_ready_for_chat's `detail`, set by _admin_chat_gate - e.g. "Your
// OpenAI account has reached its usage limit. It resets in about 27 hours."). A
// dedicated accessor, same shape as billingNoticeOf above, so a caller that only
// wants "what do I tell the customer" never has to know the raw {ready, reason,
// detail} shape checkReady() resolves to.
//
// Scoped to container_provisioning ALONE: "subscription_suspended" has its own
// dedicated copy (suspensionNotice/SUSPENDED_FALLBACK in steps.js) with a Renew
// call to action, which is wrong for this reason (nothing to renew via US when
// the customer's OWN LLM account merely ran out of quota) - a caller must not
// paint this detail into that banner's "Chat is paused" framing.
//
// "llm_credentials" is deliberately NOT handled here, and re-adding it is a
// REGRESSION (pinned by readiness.spec.js). It used to return a fixed sentence
// as a stopgap, from before needsLlmConnection() and the "No AI connected"
// banner existed. Two accessors firing for one reason meant the caller rendered
// two banners for the same state, and the generic CTA-less one won the v-else-if
// race in ChatView - so the customer whose AI is disconnected got "Chat may not
// work yet" with no way back to the AI models pane, which is the exact case the
// dedicated banner was built for. One reason, one accessor: this one answers
// "what did the backend say", needsLlmConnection() below answers "is there an
// AI attached at all".
// "authority_repair_required" (review plan 04 P0-6) shares this accessor and the
// same generic, CTA-less "Chat may not work yet" banner: admin found the
// customer's serving container ambiguous/invalid and paged support, so the ONLY
// safe thing this UI can do is show admin's own reassurance ("your payment is
// safe, please don't retry") - never a Renew or Reconnect action, both of which
// could make it worse. Like container_provisioning it carries the backend's own
// `detail`, so it belongs here rather than on any billing/AI-connect banner.
export async function readinessDetailOf() {
	const r = await checkReady();
	if (!r || r.ready) return "";
	if (r.reason === "container_provisioning" || r.reason === "authority_repair_required")
		return r.detail || "";
	return "";
}

// True when the workspace is not chat-ready specifically because it has no usable
// LLM credential - the customer disconnected their AI, or the credential the
// workspace was using expired or was revoked. A sibling accessor rather than a
// widening of readinessDetailOf above, because that one answers "what did the
// backend say about this" and is_ready_for_chat sends no `detail` for this reason:
// there is nothing to quote, only a state to name.
//
// This deliberately does NOT belong in NOT_ONBOARDED_REASONS (see the comment
// there). The full-screen gate poster is for a workspace that was never set up;
// this one HAS been set up and merely has no AI attached right now, so it keeps its
// chat history, its settings and every other route, and gets a banner instead.
export async function needsLlmConnection() {
	const r = await checkReady();
	return !!(r && !r.ready && r.reason === "llm_credentials");
}
