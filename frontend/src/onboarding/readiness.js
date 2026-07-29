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

// Fail-open: if the backend check THROWS, treat the workspace as ready so a
// flaky/500 check never strands a real user. (Note this only covers thrown
// errors — a returned {ready:false} is a real verdict, handled below.)
export function checkReady() {
	if (!readyPromise) {
		readyPromise = isReadyForChat().catch(() => ({ ready: true }));
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
// never completed onboarding" — the FIRST setup step for each mode:
//   - "signup"             managed: no admin api_key yet (wizard not started)
//   - "selfhost_connection" self-host: no validated openclaw connection yet
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
const NOT_ONBOARDED_REASONS = new Set([
	"signup",
	"selfhost_connection",
	"llm_pool_provisioning",
	"llm_provisioning",
	"llm_setup",
]);

// True only when the workspace has NOT completed onboarding at all — the single
// case the full-screen gate poster is for. A ready workspace, a fail-open
// (thrown) result, or a merely-degraded one (llm_credentials) all return false.
export async function needsOnboarding() {
	const resp = await checkReady();
	if (isOnboardComplete(resp)) return false;
	return NOT_ONBOARDED_REASONS.has(resp && resp.reason);
}

// Billing banner payload from the same memoized verdict - no extra round-trip.
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
export async function readinessDetailOf() {
	const r = await checkReady();
	if (!r || r.ready) return "";
	if (r.reason === "container_provisioning") return r.detail || "";
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
