import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// jarvis_onboarding_banner.bundle.js is a plain script loaded via
// app_include_js (not an ES module), so it cannot be `import`ed the way the
// widget's *.mjs files are - it can only be `require`d as CommonJS (see the
// test-only `module.exports` guard at the bottom of that file). require()
// executes the file's top level immediately, which touches two browser
// globals before nudgeVariant() is ever reachable: `window` (the
// double-injection guard, `window.__jarvisOnboardingBanner`) and `document`
// (the DOMContentLoaded fallback at the very end, since neither
// `window.frappe.router` nor `window.$` is set here). Both are stubbed to the
// bare minimum needed to survive that top level without touching a DOM.
//
// `window` is aliased to `globalThis` itself (as it is in a real browser, not
// a separate object) because the bundle reads the frappe.boot.* fields via a
// bare `frappe` identifier after a `window.frappe &&` guard - true only when
// `window` and the global scope are the same object.
globalThis.window = globalThis;
globalThis.document = { addEventListener: () => {} };
globalThis.frappe = undefined;

const require = createRequire(import.meta.url);
const {
  nudgeVariant,
  isSuppressedReason,
} = require("./jarvis_onboarding_banner.bundle.js");

// `hasBeenReady` defaults to `undefined` - an absent jarvis_has_been_ready key
// on the boot object, exactly like an older boot payload that predates the
// flag - rather than to `false`, so a caller that does not pass it exercises
// the "missing" case, not the "explicitly false" one. Pass it explicitly
// wherever the distinction matters.
function setReason(reason, agentName, hasBeenReady) {
  globalThis.frappe = {
    boot: {
      jarvis_ready_reason: reason,
      jarvis_agent_name: agentName,
      jarvis_has_been_ready: hasBeenReady,
    },
  };
}

test("nudgeVariant: subscription_suspended renews via billing, never the wizard", () => {
  setReason("subscription_suspended", "Nova");
  const v = nudgeVariant();
  assert.equal(v.ctaLabel, "Renew plan →");
  assert.equal(v.href, "/jarvis/billing");
  assert.match(v.text, /Nova/);
  assert.doesNotMatch(v.href, /onboarding/);
});

test("nudgeVariant: reconnect_required carries the reconnect intent flag, not a bare wizard link", () => {
  setReason("reconnect_required", "Nova");
  const v = nudgeVariant();
  assert.equal(v.ctaLabel, "Reconnect →");
  assert.equal(v.href, "/jarvis/onboarding?reconnect=1");
  assert.match(v.href, /[?&]reconnect=1(&|$)/);
  assert.match(v.text, /reconnecting/);
  assert.doesNotMatch(v.text, /Hey/);
});

test("nudgeVariant: llm_pool_provisioning/llm_provisioning + jarvis_has_been_ready true points at AI models settings", () => {
  for (const reason of ["llm_pool_provisioning", "llm_provisioning"]) {
    setReason(reason, "Nova", true);
    const v = nudgeVariant();
    assert.equal(v.ctaLabel, "Check AI models →", `reason=${reason}`);
    assert.equal(v.href, "/jarvis/?settings=aimodels", `reason=${reason}`);
    assert.match(v.text, /Nova/, `reason=${reason}`);
    assert.doesNotMatch(v.href, /onboarding/, `reason=${reason}`);
  }
});

test("nudgeVariant: llm_pool_provisioning/llm_provisioning + jarvis_has_been_ready false keeps the wizard pitch", () => {
  for (const reason of ["llm_pool_provisioning", "llm_provisioning"]) {
    setReason(reason, "Nova", false);
    const v = nudgeVariant();
    assert.equal(v.ctaLabel, "Set up Jarvis →", `reason=${reason}`);
    assert.equal(v.href, "/jarvis/onboarding", `reason=${reason}`);
  }
});

test("nudgeVariant: llm_pool_provisioning/llm_provisioning + jarvis_has_been_ready absent keeps the wizard pitch", () => {
  // Absent (older boot payload, or the boot-time try/except's fail-safe False
  // never having reached this session's cached boot object) must behave like
  // "not established", never like "established" - the safer failure mode when
  // the disambiguator itself is missing.
  for (const reason of ["llm_pool_provisioning", "llm_provisioning"]) {
    setReason(reason, "Nova"); // hasBeenReady omitted -> undefined
    const v = nudgeVariant();
    assert.equal(v.ctaLabel, "Set up Jarvis →", `reason=${reason}`);
    assert.equal(v.href, "/jarvis/onboarding", `reason=${reason}`);
  }
});

test("nudgeVariant: llm_credentials and llm_apply_stuck are unchanged", () => {
  setReason("llm_credentials", "Nova");
  let v = nudgeVariant();
  assert.equal(v.ctaLabel, "Reconnect a model →");
  assert.equal(v.href, "/jarvis/?settings=aimodels");

  setReason("llm_apply_stuck", "Nova");
  v = nudgeVariant();
  assert.equal(v.ctaLabel, "Retry");
  assert.equal(v.action, "resync");
});

test("nudgeVariant: reasons account.py guarantees have no history keep the wizard pitch", () => {
  for (const reason of [
    "signup",
    "llm_setup",
    "llm_rejected",
    "readiness_unconfirmed",
    "",
    "some_future_reason",
  ]) {
    setReason(reason, "Nova");
    const v = nudgeVariant();
    assert.equal(v.ctaLabel, "Set up Jarvis →", `reason=${reason}`);
    assert.equal(v.href, "/jarvis/onboarding", `reason=${reason}`);
  }
});

test("nudgeVariant: white-label - only the never-set-up pitch says literal Jarvis", () => {
  setReason("subscription_suspended", "Nova");
  assert.equal(nudgeVariant().name, "Nova");

  setReason("reconnect_required", "Nova");
  assert.equal(nudgeVariant().name, "Nova");

  setReason("llm_pool_provisioning", "Nova", true);
  assert.equal(nudgeVariant().name, "Nova");

  setReason("signup", "Nova");
  assert.equal(nudgeVariant().name, "Jarvis");
});

test("isSuppressedReason: the outage/incident reasons hide the nudge entirely", () => {
  for (const reason of [
    "llm_applying",
    "container_provisioning",
    "container_unavailable",
    "authority_repair_required",
    "site_replaced",
  ]) {
    assert.equal(isSuppressedReason(reason), true, `reason=${reason}`);
  }
});

test("isSuppressedReason: every reason with its own nudgeVariant() is never suppressed", () => {
  for (const reason of [
    "llm_credentials",
    "llm_apply_stuck",
    "subscription_suspended",
    "reconnect_required",
    "llm_pool_provisioning",
    "llm_provisioning",
    "signup",
    "llm_setup",
    "llm_rejected",
    "readiness_unconfirmed",
    "",
    "some_future_reason",
  ]) {
    assert.equal(isSuppressedReason(reason), false, `reason=${reason}`);
  }
});

// Route-drift guards: nudgeVariant()'s destinations are strings hand-kept in
// sync with the SPA router rather than imported (this bundle cannot import
// from frontend/src - see the module comment in the bundle itself), so a
// route rename on the SPA side would silently 404 the CTA instead of failing
// anywhere. Reading the source files as text and asserting on the literal
// each constant was copied from turns that into a loud CI failure.
const repoRoot = fileURLToPath(new URL("../../..", import.meta.url));

test("BILLING_URL: frontend/src/router/index.js still registers /billing", () => {
  const routerSrc = readFileSync(
    `${repoRoot}/frontend/src/router/index.js`,
    "utf8"
  );
  assert.match(routerSrc, /path:\s*"\/billing"/);
});

test("RECONNECT_INTENT_URL: frontend/src/onboarding/readiness.js still exports the same literal", () => {
  const readinessSrc = readFileSync(
    `${repoRoot}/frontend/src/onboarding/readiness.js`,
    "utf8"
  );
  assert.match(
    readinessSrc,
    /RECONNECT_INTENT_URL\s*=\s*"\/jarvis\/onboarding\?reconnect=1"/
  );
});
