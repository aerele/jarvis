import { test } from "node:test";
import assert from "node:assert/strict";
import {
  classifyReadiness,
  degradedMessage,
  SUSPENDED_FALLBACK,
} from "./panel_readiness.mjs";

test("classifyReadiness: ready is ready regardless of a leftover reason", () => {
  assert.equal(classifyReadiness({ ready: true, reason: null }), "ready");
  assert.equal(classifyReadiness({ ready: true }), "ready");
});

test("classifyReadiness: never-onboarded reasons gate the whole panel", () => {
  for (const reason of [
    "signup",
    "selfhost_connection",
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
