import { test } from "node:test";
import assert from "node:assert/strict";
import {
  classifyReadiness,
  degradedMessage,
  degradedActionable,
  shouldWarnWorkers,
  SUSPENDED_FALLBACK,
} from "./panel_readiness.mjs";

test("classifyReadiness: ready is ready regardless of a leftover reason", () => {
  assert.equal(classifyReadiness({ ready: true, reason: null }), "ready");
  assert.equal(classifyReadiness({ ready: true }), "ready");
});

test("classifyReadiness: never-onboarded reasons gate the whole panel", () => {
  for (const reason of [
    "signup",
    "llm_pool_provisioning",
    "llm_provisioning",
  ]) {
    assert.equal(classifyReadiness({ ready: false, reason }), "gate");
  }
});

test("classifyReadiness: llm_credentials degrades, it does NOT gate", () => {
  // This is the regression this module exists to prevent: llm_credentials
  // also fires for an already-onboarded workspace whose creds later expire,
  // so it must never trigger the full "finish setting up" nudge.
  assert.equal(
    classifyReadiness({ ready: false, reason: "llm_credentials" }),
    "degraded"
  );
});

test("classifyReadiness: an unrecognised not-ready reason degrades rather than gates", () => {
  // container_provisioning / subscription_suspended and anything future or
  // unknown fall here too - only the explicit never-onboarded set gates.
  assert.equal(
    classifyReadiness({ ready: false, reason: "container_provisioning" }),
    "degraded"
  );
  assert.equal(
    classifyReadiness({ ready: false, reason: "subscription_suspended" }),
    "degraded"
  );
  assert.equal(
    classifyReadiness({ ready: false, reason: "something_new" }),
    "degraded"
  );
});

test("classifyReadiness: fails OPEN on a missing/thrown response", () => {
  assert.equal(classifyReadiness(null), "ready");
  assert.equal(classifyReadiness(undefined), "ready");
});

test("degradedMessage: prefers the backend's own detail sentence", () => {
  assert.equal(
    degradedMessage({
      ready: false,
      reason: "container_provisioning",
      detail: "Starting up.",
    }),
    "Starting up."
  );
});

test("degradedMessage: falls back to generic copy when there is no detail", () => {
  const msg = degradedMessage({ ready: false, reason: "llm_credentials" });
  assert.ok(msg.length > 0);
  assert.equal(
    degradedMessage({ ready: false, reason: "llm_credentials", detail: "" }),
    msg
  );
});

// A lapsed subscription must not fall through to the generic "ask your
// administrator" line: no administrator can reconnect their way out of a
// billing problem. Mirrors steps.js's suspensionNotice.
test("degradedMessage: a suspended subscription gets the renewal line, not the generic one", () => {
  assert.equal(
    degradedMessage({ ready: false, reason: "subscription_suspended" }),
    SUSPENDED_FALLBACK
  );
  assert.match(SUSPENDED_FALLBACK, /Renew/);
  // admin's own sentence still wins when it has one
  assert.equal(
    degradedMessage({
      ready: false,
      reason: "subscription_suspended",
      detail: "Your plan ended on 1 August.",
    }),
    "Your plan ended on 1 August."
  );
});

// A control-plane outage on a workspace nothing has confirmed yet
// (account.py::_admin_unreachable_verdict). It used to fall to the generic line,
// which tells the customer to ask an administrator to finish reconnecting - there
// is nothing to reconnect and no administrator can help.
test("degradedMessage: an unconfirmed readiness verdict says retry, not 'ask your administrator'", () => {
  const generic = degradedMessage({ ready: false, reason: "llm_credentials" });
  const msg = degradedMessage({
    ready: false,
    reason: "readiness_unconfirmed",
  });
  assert.notEqual(msg, generic);
  assert.match(msg, /try again/i);
  assert.equal(
    degradedMessage({
      ready: false,
      reason: "readiness_unconfirmed",
      detail: "We couldn't confirm your workspace is ready yet.",
    }),
    "We couldn't confirm your workspace is ready yet."
  );
});

// P2-02: this widget is white-labelled, so the unconfirmed sentence must name the
// configured agent, not a hardcoded "Jarvis". The caller passes the boot name;
// "Jarvis" is only the fallback for a workspace that set none.
test("degradedMessage: the unconfirmed sentence uses the configured agent name", () => {
  const named = degradedMessage(
    { ready: false, reason: "readiness_unconfirmed" },
    "Aria"
  );
  assert.match(named, /Aria/);
  assert.doesNotMatch(named, /Jarvis/);
  // No name configured -> the fallback brand.
  assert.match(
    degradedMessage({ ready: false, reason: "readiness_unconfirmed" }, ""),
    /Jarvis/
  );
  assert.match(
    degradedMessage({ ready: false, reason: "readiness_unconfirmed" }),
    /Jarvis/
  );
});

// The reason set belongs to account.py. Printing a raw detail for a reason this
// module does not recognise would leak whatever wording a future backend change
// happens to attach, into a banner written for a different situation.
test("degradedMessage: never prints a raw detail for an unrecognised reason", () => {
  const generic = degradedMessage({ ready: false, reason: "llm_credentials" });
  assert.equal(
    degradedMessage({
      ready: false,
      reason: "something_new",
      detail: "internal: shard 4 unreachable",
    }),
    generic
  );
});

// jarvis#825: a stuck apply is a DEGRADED state on an established workspace, never
// the whole-panel setup gate. If it ever gated, llm_apply_stuck was wrongly added
// to NOT_ONBOARDED_REASONS - the same regression the llm_credentials test guards.
test("classifyReadiness: llm_apply_stuck degrades, it does NOT gate", () => {
  assert.equal(
    classifyReadiness({ ready: false, reason: "llm_apply_stuck" }),
    "degraded"
  );
});

// The member (non-admin) sees the state named and is pointed at their admin - the
// Retry endpoint is admin-gated, so a member button would only 403. Distinct from
// the generic "ask your administrator to finish reconnecting" line.
test("degradedMessage: a stuck apply tells a member to ask their admin to retry", () => {
  const msg = degradedMessage({ ready: false, reason: "llm_apply_stuck" });
  assert.match(msg, /didn't finish/i);
  assert.match(msg, /administrator/i);
});

// An admin gets actionable copy plus a Retry CTA carrying an `action` (handled in
// the panel), NOT an `href` - the retry re-drives the config in place rather than
// navigating away. This is the one reason besides llm_credentials that opts into a
// CTA at all.
test("degradedActionable: an admin gets a Retry action for a stuck apply", () => {
  const out = degradedActionable(
    { ready: false, reason: "llm_apply_stuck" },
    "Jarvis",
    true
  );
  assert.match(out.text, /Retry to finish it/);
  assert.equal(out.cta.label, "Retry");
  assert.equal(out.cta.action, "resync");
  assert.equal(out.cta.href, undefined);
});

// A member never gets the button (canOnboard gates it off before the panel even
// asks), so degradedActionable must return no CTA for them and keep the member copy.
test("degradedActionable: a member gets no CTA for a stuck apply", () => {
  const out = degradedActionable(
    { ready: false, reason: "llm_apply_stuck" },
    "Jarvis",
    false
  );
  assert.equal(out.cta, null);
  assert.match(out.text, /administrator/i);
});

// worker_warning is a separate, additive signal from the ready/gate/degraded
// verdict: a workspace can be fully "ready" and still carry a worker warning.
test("shouldWarnWorkers: true only when the backend flag is explicitly true", () => {
  assert.equal(shouldWarnWorkers({ ready: true, worker_warning: true }), true);
  assert.equal(
    shouldWarnWorkers({
      ready: false,
      reason: "llm_credentials",
      worker_warning: true,
    }),
    true
  );
  assert.equal(
    shouldWarnWorkers({ ready: true, worker_warning: false }),
    false
  );
  assert.equal(shouldWarnWorkers({ ready: true }), false);
});

// Fails CLOSED, the opposite of classifyReadiness's fail-open: a missing or
// thrown response must never manufacture a warning nobody confirmed.
test("shouldWarnWorkers: fails CLOSED on a missing response", () => {
  assert.equal(shouldWarnWorkers(null), false);
  assert.equal(shouldWarnWorkers(undefined), false);
});
