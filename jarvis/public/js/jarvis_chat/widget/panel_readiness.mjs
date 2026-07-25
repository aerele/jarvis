// Chat-readiness classification for the floating widget panel.
//
// DUPLICATED from frontend/src/onboarding/readiness.js by necessity, not by
// choice: this widget is plain Vue + .mjs served straight into Desk (see
// jarvis_widget.bundle.js) and cannot import from frontend/src or use the "@/"
// alias the SPA's Vite build resolves. Keep NOT_ONBOARDED_REASONS and the
// fail-open contract below in sync with that file BY HAND. See its comments
// for the full reasoning behind each reason's placement - repeated here only
// to the extent this module needs it to classify a verdict.

// Reasons meaning the workspace has NEVER completed onboarding at all - the
// only case that should replace the whole panel with a setup nudge.
//
// Deliberately excludes "llm_credentials": that reason ALSO fires when an
// already-working workspace's LLM creds later expire or rotate, and hard
// gating such a workspace out of its own chat is wrong. It falls through to
// "degraded" instead, which keeps the chat usable and only warns.
//
// KNOWN LIMITATION, shared with the SPA rather than introduced here.
// account.py returns "llm_credentials" for BOTH of those situations: creds that
// expired (account.py:185) and a subscription/oauth tenant that never connected
// at all, where llm_oauth_connected_at was never stamped (account.py:203). The
// two are indistinguishable from this side, so a never-connected tenant is
// under-gated: it gets the banner rather than the nudge. Gating on it instead
// would trade that for the worse failure, ejecting working workspaces on a
// routine credential rotation. Disambiguating needs a distinct reason code from
// account.py, which is a backend change and deliberately out of scope here.
// Whichever way it is fixed, fix frontend/src/onboarding/readiness.js in the
// same change: divergence between these two files is exactly the bug this
// module exists to close.
const NOT_ONBOARDED_REASONS = new Set([
  "signup",
  "selfhost_connection",
  "llm_pool_provisioning",
  "llm_provisioning",
]);

// Three-way verdict the panel renders around:
//  - "ready"    chat works, render the panel as normal.
//  - "gate"     never onboarded, replace the body with the setup nudge.
//  - "degraded" onboarded once but not currently chat-ready (e.g. expired
//               creds, a paused subscription); keep the composer, add a
//               banner explaining a send may fail.
//
// `resp` is whatever jarvis.account.is_ready_for_chat returned. Fail OPEN to
// "ready" on a missing/falsy response - a thrown or timed-out check must
// never strand a real user behind a scary gate (mirrors readiness.js's own
// checkReady(), which catches the backend call into {ready: true}).
export function classifyReadiness(resp) {
  if (!resp || resp.ready) return "ready";
  return NOT_ONBOARDED_REASONS.has(resp.reason) ? "gate" : "degraded";
}

// Mirrors steps.js's SUSPENDED_FALLBACK verbatim. A lapsed subscription needs a
// renewal call to action, not the generic "ask your administrator" line: there
// is nothing an administrator can reconnect when the problem is billing.
export const SUSPENDED_FALLBACK =
  "Your subscription is no longer active. Renew to restore access to Jarvis.";

const GENERIC_DEGRADED =
  "Jarvis isn't fully set up, so replies may fail. Ask your administrator to finish reconnecting it.";

// Copy for the degraded banner, structured to match steps.js's suspensionNotice
// so the two surfaces cannot drift apart on the same verdict.
//
// Per reason, because a single "use detail if present" rule got this wrong:
//   subscription_suspended  admin's sentence, else the RENEWAL line. account.py
//                           populates `detail` for this reason as well as for
//                           container_provisioning, so falling through to the
//                           generic administrator line stranded a billing
//                           problem behind advice that cannot fix it.
//   container_provisioning  admin's sentence when it has one (this is the path
//                           that carries the quota and cooldown wording), else
//                           the generic line.
//   anything else           the generic line. Do NOT print a raw `detail` for
//                           an unrecognised reason: the reason set is owned by
//                           account.py and a future addition would leak
//                           whatever wording it happens to carry.
export function degradedMessage(resp) {
  const reason = (resp && resp.reason) || "";
  const detail = (resp && resp.detail) || "";
  if (reason === "subscription_suspended") return detail || SUSPENDED_FALLBACK;
  if (reason === "container_provisioning") return detail || GENERIC_DEGRADED;
  return GENERIC_DEGRADED;
}
