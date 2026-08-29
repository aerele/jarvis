// Chat-readiness classification for the floating widget panel.
//
// DUPLICATED from frontend/src/onboarding/readiness.js by necessity, not by
// choice: this widget is plain Vue + .mjs served straight into Desk (see
// jarvis_widget.bundle.js) and cannot import from frontend/src or use the "@/"
// alias the SPA's Vite build resolves. Keep NOT_ONBOARDED_REASONS and the
// fail-open contract below in sync with that file BY HAND. See its comments
// for the full reasoning behind each reason's placement - repeated here only
// to the extent this module needs it to classify a verdict.

import { AI_MODELS_SETTINGS_URL } from "./config.mjs";

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
// "llm_setup" is the server-decided HARD variant of llm_credentials (creds
// missing + nothing ever synced + subscription never Active): a half-finished
// signup, where chat cannot work at all.
// "llm_rejected" (jarvis#757): a first pool/direct sync the server explicitly
// refused. readiness.js lists it here for the same reason as the two
// provisioning reasons (a first sync that never succeeded), and the desk
// onboarding banner routes it to setup too, so the widget must agree or the two
// desk surfaces show contradictory recovery paths for one verdict (jarvis
// review). "readiness_unconfirmed" is deliberately NOT added: it is transient
// (the control plane could not answer yet), and degradedMessage below gives it
// a dedicated "try again shortly" line that a one-way setup gate would bury.
// "llm_applying" (jarvis C2) is deliberately ABSENT too, same as in
// readiness.js: the server only ever returns it for a workspace that has
// already been confirmed established (account.py's _provisioning_verdict), so
// it can never fire on a genuinely never-onboarded tenant. Falling through to
// "degraded" below is the whole point - it keeps the customer IN the panel
// with a quiet heads-up instead of replacing it with the setup nudge. Keep
// this file and readiness.js's own "deliberately ABSENT" note in sync BY HAND -
// do not "fix" this by adding it to the set.
// "llm_apply_stuck" (jarvis#825) is deliberately ABSENT too, same reasoning: an
// established workspace whose apply hung falls through to "degraded" and gets an
// honest banner + admin Retry (degradedActionable below), never the whole-panel
// setup gate. Keep this in sync with readiness.js by hand.
const NOT_ONBOARDED_REASONS = new Set([
  "signup",
  "llm_setup",
  "llm_pool_provisioning",
  "llm_provisioning",
  "llm_rejected",
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

// account.py could not reach its control plane AND this workspace has never been
// confirmed chat-ready, so nobody knows whether a container is serving it
// (jarvis/account.py::_admin_unreachable_verdict). Retryable by construction:
// this is the absence of a verdict, not one. Without a case of its own it fell
// to GENERIC_DEGRADED, which tells the customer to ask an administrator to
// finish reconnecting - wrong twice over, since nothing is misconfigured and no
// administrator can shorten an outage.
//
// Named for the configured agent (P2-02): this surface is white-labelled, so a
// hardcoded "Jarvis" leaks our brand onto a customer who renamed the assistant.
// The caller passes window.frappe.boot.jarvis_agent_name; "Jarvis" is the
// fallback for a workspace that set none.
const unconfirmedDegraded = (agentName) =>
  `We couldn't confirm ${agentName} is ready, so replies may fail. This usually clears in a moment - try again shortly.`;

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
//   readiness_unconfirmed   the "try again shortly" line. account.py always
//                           attaches a detail here, but the fallback is the
//                           retryable one either way - never the generic
//                           "ask your administrator", which names a person who
//                           cannot help with a control-plane outage.
//   llm_applying            (jarvis C2) a quiet, honest heads-up - NOT the
//                           generic "ask your administrator" line, which would
//                           be actively wrong here: nothing is misconfigured,
//                           an established workspace's own first pool/direct
//                           apply is simply still converging. Same wording as
//                           ChatView.vue's banner (FACT B: a first direct->pool
//                           transition bounces the container, so this must NOT
//                           promise chat keeps working uninterrupted).
//   anything else           the generic line. Do NOT print a raw `detail` for
//                           an unrecognised reason: the reason set is owned by
//                           account.py and a future addition would leak
//                           whatever wording it happens to carry.
const APPLYING_DEGRADED =
  "Updating your AI configuration. Chat may be briefly unavailable while this finishes.";

//   llm_apply_stuck         (jarvis#825) the llm_applying window aged out without
//                           the apply ever finishing - a genuinely stuck apply,
//                           not a still-converging one. The MEMBER copy here names
//                           the state and points at an admin; the ADMIN gets the
//                           actionable copy + Retry via degradedActionable below,
//                           the one reason besides llm_credentials that opts into a
//                           CTA. Same "your last AI update didn't finish" framing
//                           as ChatView.vue's SPA banner - keep the two in sync BY
//                           HAND.
const APPLY_STUCK_MEMBER =
  "Your last AI update didn't finish, so replies may fail. Ask your administrator to retry it.";
const APPLY_STUCK_ADMIN =
  "Your last AI update didn't finish, so replies may fail. Retry to finish it.";

export function degradedMessage(resp, agentName = "Jarvis") {
  const reason = (resp && resp.reason) || "";
  const detail = (resp && resp.detail) || "";
  const brand = (agentName || "").trim() || "Jarvis";
  if (reason === "subscription_suspended") return detail || SUSPENDED_FALLBACK;
  if (reason === "container_provisioning") return detail || GENERIC_DEGRADED;
  if (reason === "readiness_unconfirmed")
    return detail || unconfirmedDegraded(brand);
  if (reason === "llm_applying") return APPLYING_DEGRADED;
  if (reason === "llm_apply_stuck") return APPLY_STUCK_MEMBER;
  return GENERIC_DEGRADED;
}

// Role-aware wrapper around degradedMessage() for the ONE reason an admin can
// fix from here: "llm_credentials", a workspace whose AI was disconnected or
// whose credential expired, so no model is attached at all. An admin gets
// actionable "no AI connected" copy plus a link to the AI models settings pane;
// a member cannot act on it, so they keep degradedMessage()'s member wording
// with no button.
//
// ALLOWLIST, not a denylist (jarvis review). Every OTHER reason keeps
// degradedMessage()'s own copy and gets no CTA. Listing instead the reasons that
// have dedicated copy silently handed this "Connect a model" button - and threw
// away the reason's real detail - to "llm_rejected" (a config the server
// refused, where a model IS attached; now gated to setup above) and to any
// reason account.py grows later. Only the reason this button actually fixes
// opts in.
//
// The "No AI connected..." string is duplicated verbatim from ChatView.vue's
// noAiConnectedMessage (the SPA banner for the same condition). Keep the two in
// sync BY HAND, same as NOT_ONBOARDED_REASONS above.
export function degradedActionable(
  resp,
  agentName = "Jarvis",
  isAdmin = false
) {
  const reason = (resp && resp.reason) || "";
  if (isAdmin && reason === "llm_credentials") {
    return {
      text: "No AI connected. Connect a model to start chatting again.",
      cta: { label: "Connect a model", href: AI_MODELS_SETTINGS_URL },
    };
  }
  // jarvis#825: a stuck apply an admin CAN act on from here - Retry re-drives the
  // saved config in place (resyncLlm), so the CTA carries an `action` the panel
  // handles locally, not an `href` that would navigate away from the chat.
  if (isAdmin && reason === "llm_apply_stuck") {
    return {
      text: APPLY_STUCK_ADMIN,
      cta: { label: "Retry", action: "resync" },
    };
  }
  return { text: degradedMessage(resp, agentName), cta: null };
}

// A SEPARATE, additive signal from the ready/gate/degraded verdict above:
// is_ready_for_chat now also returns worker_warning (bool) on an otherwise-usable
// workspace whose background workers are under-provisioned. It is deliberately
// non-blocking - unlike a degraded verdict, it never gates the composer or
// replaces the panel - so it is read independently of classifyReadiness() rather
// than folded into its three-way return.
//
// Fails CLOSED (false) on a missing/thrown response, the opposite of
// classifyReadiness's fail-OPEN-to-"ready": a check that could not run should
// produce no banner, not manufacture a warning nobody confirmed. The panel's
// caller still wraps the isReadyForChat() call in its own try/catch (mirroring
// how it already resolves `readiness` on a thrown fetch), so this only needs to
// handle a resp that is present but shaped unexpectedly.
export function shouldWarnWorkers(resp) {
  return !!(resp && resp.worker_warning);
}
