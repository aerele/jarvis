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
