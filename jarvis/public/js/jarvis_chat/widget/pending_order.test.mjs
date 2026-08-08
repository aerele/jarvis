import { test } from "node:test";
import assert from "node:assert/strict";
import { comparePendingCards, sortPendingCards } from "./pending_order.mjs";

// The Desk widget shipped the wrong-write bug because its cards were built
// without expires_at, so its correct-looking comparator sorted by token alone.
// These pin the comparator's real behaviour with real inputs (a source grep
// would not have caught that); chat_stream.test.mjs pins that the field actually
// reaches the item.

test("orders by expires_at ascending, earliest-minted is number 1", () => {
  const out = sortPendingCards([
    { token: "z", expires_at: 200 },
    { token: "a", expires_at: 100 },
  ]);
  assert.deepEqual(
    out.map((c) => c.token),
    ["a", "z"]
  );
});

test("tie-breaks equal expires_at by token in code-unit order, matching the server", () => {
  // 'A' (0x41) < 'z' (0x7A) by code unit; a locale compare would disagree - the
  // exact divergence that renumbers a card between screen and server.
  const out = sortPendingCards([
    { token: "z9", expires_at: 100 },
    { token: "A0", expires_at: 100 },
  ]);
  assert.deepEqual(
    out.map((c) => c.token),
    ["A0", "z9"]
  );
});

test("two cards with differing expires_at do NOT collapse to token order", () => {
  // Regression for the shipped bug: if expires_at is present it must dominate.
  // 'A0' has the LATER expiry, so token order (A0 first) must lose to mint order.
  const out = sortPendingCards([
    { token: "A0", expires_at: 200 },
    { token: "z9", expires_at: 100 },
  ]);
  assert.deepEqual(
    out.map((c) => c.token),
    ["z9", "A0"]
  );
});

test("treats a missing expires_at as 0 without throwing", () => {
  const out = sortPendingCards([{ token: "b", expires_at: 5 }, { token: "a" }]);
  assert.deepEqual(
    out.map((c) => c.token),
    ["a", "b"]
  );
});
