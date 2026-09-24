import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  comparePendingCards,
  dropDiscarded,
  isRecentCard,
  keepEarlier,
  markCardsEarlier,
  sortPendingCards,
  typedApprovalHint,
} from "./pending_order.mjs";

// The Desk widget shipped the wrong-write bug because its cards were built
// without expires_at, so its correct-looking comparator sorted by token alone.
// These pin the comparator's real behaviour with real inputs (a source grep
// would not have caught that); chat_stream.test.mjs pins that the field actually
// reaches the item.

test("orders by created_at ascending, earliest-minted is number 1", () => {
  const out = sortPendingCards([
    { token: "z", created_at: 200 },
    { token: "a", created_at: 100 },
  ]);
  assert.deepEqual(
    out.map((c) => c.token),
    ["a", "z"]
  );
});

test("tie-breaks equal created_at by token in code-unit order, matching the server", () => {
  // 'A' (0x41) < 'z' (0x7A) by code unit; a locale compare would disagree - the
  // exact divergence that renumbers a card between screen and server.
  const out = sortPendingCards([
    { token: "z9", created_at: 100 },
    { token: "A0", created_at: 100 },
  ]);
  assert.deepEqual(
    out.map((c) => c.token),
    ["A0", "z9"]
  );
});

test("two cards with differing created_at do NOT collapse to token order", () => {
  // Regression for the shipped bug: if created_at is present it must dominate.
  // 'A0' has the LATER created_at, so token order (A0 first) must lose to mint order.
  const out = sortPendingCards([
    { token: "A0", created_at: 200 },
    { token: "z9", created_at: 100 },
  ]);
  assert.deepEqual(
    out.map((c) => c.token),
    ["z9", "A0"]
  );
});

test("created_at wins over a misleading expires_at (a later mint, earlier expiry)", () => {
  const out = sortPendingCards([
    { token: "late", created_at: 200, expires_at: 150 },
    { token: "early", created_at: 100, expires_at: 999 },
  ]);
  assert.deepEqual(
    out.map((c) => c.token),
    ["early", "late"]
  );
});

test("falls back to expires_at when created_at is missing (a mixed deploy)", () => {
  const out = sortPendingCards([
    { token: "z", expires_at: 200 },
    { token: "a", expires_at: 100 },
  ]);
  assert.deepEqual(
    out.map((c) => c.token),
    ["a", "z"]
  );
});

test("mixes a pre-P0c (expires_at only) card with a post-P0c (created_at) one correctly", () => {
  const out = sortPendingCards([
    { token: "new", created_at: 200 },
    { token: "old", expires_at: 100 },
  ]);
  assert.deepEqual(
    out.map((c) => c.token),
    ["old", "new"]
  );
});

test("treats both fields missing as 0 without throwing", () => {
  const out = sortPendingCards([{ token: "b", created_at: 5 }, { token: "a" }]);
  assert.deepEqual(
    out.map((c) => c.token),
    ["a", "b"]
  );
});

test("a typed no's discarded cards leave the stack; nothing else does", () => {
  const cards = [{ token: "a" }, { token: "b" }];
  const res = {
    typed_rejection: { discarded: [{ token: "a", position: 1 }], skipped: [] },
  };
  assert.deepEqual(
    dropDiscarded(cards, res).map((c) => c.token),
    ["b"]
  );
  assert.equal(dropDiscarded(cards, { ok: true }), cards);
  assert.deepEqual(dropDiscarded(undefined, res), []);
});

// Decision 6: a bare phrase binds only cards parked since the user last spoke, so
// the widget must never advertise one for an "Earlier" card (the SPA/PWA mirror).
test("the bare-phrase hint is only for cards parked since the user last spoke", () => {
  assert.equal(typedApprovalHint([{ token: "a" }]), 'or type "go ahead"');
  assert.equal(
    typedApprovalHint([{ token: "a", recent: false }]),
    'or type "confirm 1"'
  );
  assert.equal(
    typedApprovalHint([{ token: "a" }, { token: "b", recent: true }]),
    'or type "confirm all", or "confirm 1 and 2"'
  );
  assert.equal(
    typedApprovalHint([{ token: "a", recent: false }, { token: "b" }]),
    'or type "confirm 1 and 2"'
  );
  assert.equal(typedApprovalHint([]), "");
  assert.equal(typedApprovalHint(undefined), "");
  assert.equal(isRecentCard(null), false);
});

test("an accepted send ages only the cards the user was shown", () => {
  const cards = [{ token: "a" }, { token: "b" }];
  const out = markCardsEarlier(cards, ["a"]);
  assert.deepEqual(out.map(isRecentCard), [false, true]);
  assert.equal(isRecentCard(cards[0]), true, "returns new items, no mutation");
  assert.deepEqual(markCardsEarlier(undefined, ["a"]), []);
});

test("a resync never turns an Earlier card back into a recent one", () => {
  const merged = [
    { token: "a" },
    { token: "b" },
    { token: "c", recent: false },
  ];
  const onScreen = [{ token: "a", recent: false }];
  const staleRows = [
    { token: "a", recent: true },
    { token: "b", recent: true },
  ];
  const backstop = [{ token: "b", recent: false }];
  assert.deepEqual(
    keepEarlier(merged, onScreen, staleRows, backstop).map(isRecentCard),
    [false, false, false]
  );
  assert.deepEqual(
    keepEarlier([{ token: "d" }], onScreen, staleRows).map(isRecentCard),
    [true]
  );
});

test("Panel.vue wires the recency helpers into the hint, the send and the resync", () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, "Panel.vue"), "utf8");
  assert.match(
    src,
    /const typedApprovalHint = computed\(\(\) => hintFor\(orderedPending\.value\)\)/
  );
  const confirmed = src.indexOf("if (res?.confirmed) {");
  const marked = src.indexOf(
    "markCardsEarlier(stream.value.pending, approvalTokens)"
  );
  assert.ok(
    confirmed > -1 && marked > confirmed,
    "after the confirmed early return"
  );
  assert.match(src, /keepEarlier\(\s*\[\.\.\.byToken\.values\(\)\]/);
  assert.match(src, /v-if="!isRecentCard\(p\)"[^>]*>Earlier</);
});
