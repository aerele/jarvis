import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

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
const { nudgeVariant } = require("./jarvis_onboarding_banner.bundle.js");

function setReason(reason, agentName) {
  globalThis.frappe = {
    boot: { jarvis_ready_reason: reason, jarvis_agent_name: agentName },
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

test("nudgeVariant: reconnect_required uses honest copy, not the never-set-up pitch", () => {
  setReason("reconnect_required", "Nova");
  const v = nudgeVariant();
  assert.equal(v.ctaLabel, "Reconnect →");
  assert.equal(v.href, "/jarvis/onboarding");
  assert.match(v.text, /reconnecting/);
  assert.doesNotMatch(v.text, /Hey/);
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

  setReason("signup", "Nova");
  assert.equal(nudgeVariant().name, "Jarvis");
});
