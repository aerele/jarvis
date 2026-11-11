import { test } from "node:test";
import assert from "node:assert/strict";
import { turnErrorInfo } from "./turn_errors.mjs";

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
  ["gate-wrong-site", "This site is not the one registered for this Jarvis account. Contact support if this is wrong.", false],
  ["gate-unconfirmed", "Jarvis cannot confirm this site right now. Please try again in a minute.", true],
  ["gate-update-needed", "This site needs a Jarvis update before it can use this assistant. Contact support.", false],
  [
    "gate-site-changed",
    'This site changed since Jarvis last checked it, for example after a restore or a move. A System Manager can open Jarvis and press "Re-verify this site" to reconnect. Contact support if this is wrong.',
    false,
  ],
  ["gate-background-run", "ERP tools are not available in scheduled background runs.", false],
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
    for (const suffix of [" (blocked by jarvis-erp)", " (blocked by some-other-plugin)"]) {
      const info = turnErrorInfo(`${text}${suffix}`);
      assert.equal(info.code, code, suffix);
      assert.equal(info.headline, text);
    }
  }
});
