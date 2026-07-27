import { isReadyForChat } from "@/api.js";
import { isOnboardComplete } from "@/onboarding/steps.js";

// Shared, memoized readiness verdict. Two callers need it per page load: the
// router's first-navigation guard (bounce an already-onboarded user off a stale
// /onboarding link) and the AppShell onboarding gate (block the app with a
// poster when the workspace was NEVER set up). Sharing one in-flight promise
// keeps it to a SINGLE backend round-trip.
//
// No cache-reset helper is needed: OnboardingView hard-reloads to /jarvis/ on
// completion, which re-mounts the SPA and drops this module-level cache.
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
const NOT_ONBOARDED_REASONS = new Set([
	"signup",
	"selfhost_connection",
	"llm_pool_provisioning",
	"llm_provisioning",
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
// Deliberately scoped to ONLY container_provisioning: "subscription_suspended" has
// its own dedicated copy (suspensionNotice/SUSPENDED_FALLBACK in steps.js) with a
// Renew call to action, which is wrong for this reason (nothing to renew via US when
// the customer's OWN LLM account merely ran out of quota) - a caller must not paint
// this detail into that banner's "Chat is paused" framing.
export async function readinessDetailOf() {
	const r = await checkReady();
	if (!r || r.ready || r.reason !== "container_provisioning") return "";
	return r.detail || "";
}
