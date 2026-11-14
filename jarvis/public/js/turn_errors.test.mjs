import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { modelFailures, turnErrorInfo, waitPhrase } from "./turn_errors.mjs";

// The Desk widget (jarvis_chat/widget/Panel.vue) decides whether to offer Retry from this
// same helper, so the archived-session error must be retryable here too (and the tombstone must not be).
const WRAPPED =
  'chat.send rejected: INVALID_REQUEST: Session "agent:main:dashboard:3f2b1c9e" is archived. Restore it before starting new work.';

test("an archived session is a retryable reconnect in the Desk widget helper", () => {
  for (const text of [WRAPPED, `Error: ${WRAPPED}`]) {
    const info = turnErrorInfo(text);
    assert.equal(info.code, "session-archived");
    assert.equal(info.retryable, true);
    assert.equal(info.headline, "This chat needs to reconnect");
  }
});

test("a restart-recovery tombstone cannot heal, so it is not retryable", () => {
  const info = turnErrorInfo(
    'Session "agent:main:dashboard:3f2b1c9e" ended during restart recovery. Use /new or /reset to start a replacement session.'
  );
  assert.equal(info.code, "session-ended");
  assert.equal(info.retryable, false);
  assert.equal(info.headline, "This chat can't continue");
  assert.equal(info.hint, "Start a new chat to keep going.");
});

test("a role-profile key naming billing does not turn the archive error into a billing one", () => {
  const info = turnErrorInfo(WRAPPED.replace("agent:main:", "agent:billing:"));
  assert.equal(info.code, "session-archived");
  assert.equal(info.retryable, true);
});

// The plugin's gate blocks a turn with a plain sentence that tells the customer what to do. The
// bubble must show that sentence, not the generic "cause is not clear" copy (#2484).
const GATE_TEXTS = [
  [
    "gate-wrong-site",
    "This site is not the one registered for this Jarvis account. Contact support if this is wrong.",
    false,
  ],
  [
    "gate-unconfirmed",
    "Jarvis cannot confirm this site right now. Please try again in a minute.",
    true,
  ],
  [
    "gate-update-needed",
    "This site needs a Jarvis update before it can use this assistant. Contact support.",
    false,
  ],
  [
    "gate-site-changed",
    'This site changed since Jarvis last checked it, for example after a restore or a move. A System Manager can open Jarvis and press "Re-verify this site" to reconnect. Contact support if this is wrong.',
    false,
  ],
  [
    "gate-background-run",
    "ERP tools are not available in scheduled background runs.",
    false,
  ],
  [
    "gate-subagent",
    "ERP tools are only available in the main conversation. Return your findings to the parent agent instead.",
    false,
  ],
];

test("each gate block text is the bubble message, with Retry only where a retry can help", () => {
  for (const [code, text, retryable] of GATE_TEXTS) {
    const info = turnErrorInfo(text);
    assert.equal(info.code, code, text);
    assert.equal(info.headline, text);
    assert.equal(info.hint, "");
    assert.equal(info.retryable, retryable, text);
  }
});

test("the runtime's trailing (blocked by <plugin>) is dropped, whichever plugin names it", () => {
  for (const [code, text] of GATE_TEXTS) {
    for (const suffix of [
      " (blocked by jarvis-erp)",
      " (blocked by some-other-plugin)",
    ]) {
      const info = turnErrorInfo(`${text}${suffix}`);
      assert.equal(info.code, code, suffix);
      assert.equal(info.headline, text);
    }
  }
});

// --- one cause per model of a failover error -------------------------------------------------

const FAILOVER_CASES = JSON.parse(
  readFileSync(
    new URL("../../tests/fixtures/model_failures.json", import.meta.url),
    "utf8"
  )
);
const INSIDER = FAILOVER_CASES[0].error;

test("each failed model gets the same cause and wait as the Python side", () => {
  for (const { error, models } of FAILOVER_CASES) {
    const info = turnErrorInfo(error);
    if (!models.length) {
      assert.equal(info.models, undefined, error);
      continue;
    }
    assert.deepEqual(
      info.models.map((m) => m.code),
      models.map((m) => m.code),
      error
    );
    const parts = modelFailures(error);
    models.forEach((m, i) => {
      if (m.model) assert.equal(parts[i].model, m.model);
      assert.equal(parts[i].retryAfterS, m.retry_after_s);
    });
  }
});

test("a usage cap and a dead sign-in name both causes, offer Reconnect, and no Retry", () => {
  const info = turnErrorInfo(INSIDER, undefined, {
    admin: true,
    failedAtMs: Date.now(),
  });
  assert.equal(info.headline, "None of your models could answer");
  assert.equal(info.retryable, false);
  assert.equal(info.action, "reconnect");
  assert.equal(info.actionLabel, "Reconnect Anthropic");
  assert.deepEqual(
    info.models.map((m) => `${m.model}: ${m.reason}. ${m.next}`),
    [
      "gpt-5.6-terra: Usage limit reached. Resets in about 2 days.",
      "claude-opus-5: Anthropic sign-in expired. Reconnect it in AI models.",
    ]
  );
});

test("a member is told who can fix a dead sign-in", () => {
  const info = turnErrorInfo(INSIDER);
  assert.equal(
    info.models[1].next,
    "Ask your workspace admin to reconnect it."
  );
});

test("a usage cap whose wait has passed may have reset, so Retry returns", () => {
  const info = turnErrorInfo(INSIDER, undefined, {
    failedAtMs: Date.now() - 3 * 86400e3,
  });
  assert.equal(info.models[0].next, "It may have reset. Try again.");
  assert.equal(info.retryable, true);
});

test("a row stored cut short says the reason was not received", () => {
  const info = turnErrorInfo(`${INSIDER.slice(0, 240)}...`);
  assert.equal(info.code, "models-exhausted");
  assert.equal(info.models[1].model, "claude-\u2026");
  assert.equal(info.models[1].reason, "Reason not received");
});

test("the Claude CLI's bare expiry text is an Anthropic sign-in", () => {
  const info = turnErrorInfo(
    "Failed to authenticate: OAuth session expired and could not be refreshed"
  );
  assert.equal(info.code, "subscription-expired");
  assert.equal(info.headline, "Anthropic sign-in expired");
  assert.equal(info.action, "reconnect");
});

test("a usage cap behind a 429 is the allowance, not a burst limit", () => {
  const info = turnErrorInfo(
    "429 cooling down (last error: usage_limit_reached: The usage limit has been reached)"
  );
  assert.equal(info.code, "quota");
  assert.equal(info.retryable, false);
});

test("a cause-specific server code keeps its own copy", () => {
  assert.equal(turnErrorInfo(INSIDER, "timeout").models, undefined);
});

test("waits read as rounded minutes, hours or days", () => {
  assert.equal(waitPhrase(169907), "about 2 days");
  assert.equal(waitPhrase(3 * 3600), "about 3 hours");
  assert.equal(waitPhrase(60), "about 1 minute");
  assert.equal(waitPhrase(0), "");
});

test("without the failure time the wait is stated as it was, not counted down", () => {
  const info = turnErrorInfo(INSIDER);
  assert.equal(info.models[0].next, "Resets about 2 days after it failed.");
});

test("a single model on its usage cap says when it resets", () => {
  const text =
    "429 cooling down (last error: usage_limit_reached: The usage limit has been reached); Retry-After: 7200 seconds";
  const info = turnErrorInfo(text, undefined, { failedAtMs: Date.now() });
  assert.equal(info.code, "quota");
  assert.match(info.hint, /^Resets in about 2 hours\. /);
  assert.equal(info.retryable, false);
});

test("a system error keeps its raw text as the reason (which file, which resource)", async () => {
  const { showsRawReason } = await import("./turn_errors.mjs");
  const eperm = turnErrorInfo(
    "Error: EPERM: operation not permitted, chmod /data/agent.sqlite"
  );
  assert.equal(eperm.code, "runtime-permission");
  assert.equal(showsRawReason(eperm), true);
  assert.equal(showsRawReason(turnErrorInfo(INSIDER)), false);
  assert.equal(showsRawReason(turnErrorInfo("Please run /login")), false);
});
