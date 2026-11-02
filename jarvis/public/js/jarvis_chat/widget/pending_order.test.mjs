import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  comparePendingCards,
  discardedTokens,
  dropDiscarded,
  isRecentCard,
  keptCardMessage,
  keepEarlier,
  markCardsEarlier,
  safetyUpdateTransition,
  settledReasonMessage,
  sortPendingCards,
  typedApprovalHint,
  withExcluded,
  withoutTokens,
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
  assert.deepEqual(dropDiscarded(cards, { ok: true }), cards);
  assert.deepEqual(dropDiscarded(undefined, res), []);
});

test("discardedTokens reads send_message's typed_rejection", () => {
  const res = {
    typed_rejection: {
      discarded: [{ token: "a" }, { token: "b" }],
      skipped: [],
    },
  };
  assert.deepEqual([...discardedTokens(res)], ["a", "b"]);
  assert.equal(discardedTokens({ ok: true }).size, 0);
  assert.equal(discardedTokens(undefined).size, 0);
});

// D1: a typed "no" only drops its cards from the on-screen stack (send()'s
// dropDiscarded, below); messages.value keeps the row tool_status "pending"
// until the next load(). resyncPending's rowItems rebuild from that stale
// transcript on every poll, so a discarded token has to survive past that
// single call - not just be filtered out of the merged result once.
test("withExcluded unions new tokens into the running exclusion set", () => {
  const out = withExcluded(new Set(["a"]), ["b", "b", null, undefined]);
  assert.deepEqual([...out].sort(), ["a", "b"]);
  assert.deepEqual([...withExcluded(null, ["x"])], ["x"]);
});

test("withoutTokens drops excluded entries, leaves the rest, tolerates empty inputs", () => {
  const items = [{ token: "a" }, { token: "b" }];
  assert.deepEqual(
    withoutTokens(items, new Set(["a"])).map((c) => c.token),
    ["b"]
  );
  assert.deepEqual(withoutTokens(items, new Set()), items);
  assert.deepEqual(withoutTokens(items, null), items);
  assert.deepEqual(withoutTokens(null, new Set(["a"])), []);
});

test("a token settled locally (typed no) is never resurrected by a stale transcript row", () => {
  const settled = withExcluded(
    new Set(),
    discardedTokens({
      typed_rejection: { discarded: [{ token: "x" }], skipped: [] },
    })
  );
  const staleRowItems = [{ token: "x" }];
  const merged = [...staleRowItems]; // what resyncPending would otherwise re-admit
  assert.deepEqual(withoutTokens(merged, settled), []);
});

test("a passive and direct safety update remove the same token but enqueue only one Done notice", () => {
  const initial = {
    settledTokens: new Set(),
    safetyNoticeTokens: new Set(),
    pending: [{ token: "cancelled-a" }, { token: "safe-b" }],
  };
  const passive = safetyUpdateTransition(initial, "cancelled-a");
  const direct = safetyUpdateTransition(passive, "cancelled-a");

  assert.equal(passive.shouldEnqueue, true);
  assert.equal(direct.shouldEnqueue, false);
  assert.deepEqual(
    direct.pending.map((card) => card.token),
    ["safe-b"],
    "the valid sibling stays visible"
  );
  assert.deepEqual([...direct.settledTokens], ["cancelled-a"]);
  assert.deepEqual([...direct.safetyNoticeTokens], ["cancelled-a"]);
});

test("a direct safety update also suppresses a later passive duplicate", () => {
  const direct = safetyUpdateTransition(
    {
      settledTokens: new Set(),
      safetyNoticeTokens: new Set(),
      pending: [{ token: "cancelled-a" }, { token: "safe-b" }],
    },
    "cancelled-a"
  );
  const passive = safetyUpdateTransition(direct, "cancelled-a");

  assert.equal(direct.shouldEnqueue, true);
  assert.equal(passive.shouldEnqueue, false);
  assert.deepEqual(
    passive.pending.map((card) => card.token),
    ["safe-b"]
  );
});

test("a typed discard does not suppress a later safety-update explanation", () => {
  const out = safetyUpdateTransition(
    {
      settledTokens: new Set(["a"]),
      safetyNoticeTokens: new Set(),
      pending: [{ token: "a" }, { token: "safe-b" }],
    },
    "a"
  );
  assert.equal(out.shouldEnqueue, true);
  assert.deepEqual(
    out.pending.map((card) => card.token),
    ["safe-b"]
  );
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

test("keptCardMessage: a refusal keeps the card unless it settled it, and says why", () => {
  for (const code of ["busy", "identity_refused", "armed_run"])
    assert.match(keptCardMessage({ ok: false, reason_code: code }), /\w/, code);
  assert.equal(
    keptCardMessage({
      ok: false,
      reason_code: "executing",
      pa_status: "Executing",
    }),
    "This action is already running."
  );
  for (const res of [
    { ok: true },
    { ok: false, reason_code: "already_handled" },
    { ok: false, reason_code: "not_found" },
    { ok: false, reason_code: "safety_update" },
    { ok: false, reason_code: "failed", pa_status: "Failed" },
    { ok: false, reason_code: "stale", pa_status: "Failed" },
    { ok: false, error: { type: "InvalidConfirmation" } },
    {
      ok: false,
      error: { type: "ValidationError", message: "legacy tool failed" },
    },
    undefined,
  ])
    assert.equal(keptCardMessage(res), "");
});

test("settledReasonMessage explains a safety-update cancellation", () => {
  assert.match(
    settledReasonMessage({
      ok: false,
      reason_code: "safety_update",
      pa_status: "Cancelled",
    }),
    /agent safety update/
  );
  assert.match(
    settledReasonMessage({ ok: false, reason_code: "safety_update" }),
    /Submit the action again/
  );
  assert.equal(settledReasonMessage({ ok: false, reason_code: "stale" }), "");
});

test("Panel.vue folds a typed no's discarded tokens into resyncPending's exclusion set", () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, "Panel.vue"), "utf8");
  const settle = src.indexOf(
    "settledTokens = withExcluded(settledTokens, _discarded)"
  );
  assert.ok(
    settle > -1,
    "send() must fold discarded tokens into settledTokens"
  );
  const resync = src.indexOf("async function resyncPending(");
  const excluded = src.indexOf("withoutTokens(merged, settledTokens)", resync);
  assert.ok(
    excluded > -1,
    "resyncPending must exclude settledTokens from the merged result"
  );
});

test("Panel.vue keeps a refused card instead of resolving it away", () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, "Panel.vue"), "utf8");
  const body = src.slice(src.indexOf("async function resolvePending("));
  const kept = body.search(
    /if \(kept\) \{\s*loadError\.value = kept;\s*return;/
  );
  assert.ok(kept > -1 && body.includes("const kept = keptCardMessage("));
  assert.ok(kept < body.indexOf('kind: "action:resolved"'));
});

test("Panel.vue hides a safety-update card and keeps its Done-only message", () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, "Panel.vue"), "utf8");
  const body = src.slice(src.indexOf("async function resolvePending("));
  const settled = body.indexOf("const settled = settledReasonMessage(result)");
  const settle = body.indexOf(
    "settleSafetyUpdate({ token, message: settled })",
    settled
  );
  const resolved = body.indexOf('kind: "action:resolved"', settled);
  assert.ok(settled > -1 && settle > settled && resolved > settle);
  assert.match(src, /function dismissSettledNotice\(\)/);
  assert.match(src, /@click="dismissSettledNotice"/);
  assert.doesNotMatch(
    body.slice(settled, resolved),
    /loadError\.value = settled/
  );
});

test("Desk only admits attested cached and list cards, then shows a passive close as Done-only", () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, "Panel.vue"), "utf8");
  const resync = src.slice(
    src.indexOf("async function resyncPending(source) {"),
    src.indexOf(
      "// Auto-heal",
      src.indexOf("async function resyncPending(source) {")
    )
  );
  assert.match(resync, /m\.pending_card_safe === true/);
  assert.match(resync, /r\?\.pending_card_safe === true/);
  assert.match(resync, /removedByCleanResync/);
  assert.match(resync, /void load\(\)/);
  assert.match(src, /function observePassiveSafetyUpdates\(rows\)/);
  assert.match(src, /result\?\.reason_code !== "safety_update"/);
  const noticeAt = src.indexOf('v-if="settledNotice"');
  const nextPending = src.indexOf(
    'v-for="(p, pi) in orderedPending"',
    noticeAt
  );
  assert.ok(noticeAt > -1 && nextPending > noticeAt);
  const notice = src.slice(noticeAt, nextPending);
  assert.match(notice, />\s*Done\s*</);
  assert.doesNotMatch(notice, /Confirm|Retry|Discard/);

  const realtime = src.slice(src.indexOf("function onRealtime(payload) {"));
  assert.match(realtime, /payload\.pending_card_safe === true/);
});

test("Desk queues passive safety receipts so each Done reveals the next one", () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, "Panel.vue"), "utf8");
  assert.match(src, /const settledNoticeQueue = ref\(\[\]\)/);
  assert.match(src, /function enqueueSettledNotice\(notice\)/);
  const observe = src.slice(
    src.indexOf("function observePassiveSafetyUpdates(rows)"),
    src.indexOf(
      "// Typed approval",
      src.indexOf("function observePassiveSafetyUpdates(rows)")
    )
  );
  assert.doesNotMatch(observe, /if \(settledNotice\.value\) return/);
  assert.match(observe, /settleSafetyUpdate\(notice\)/);
  assert.match(src, /function settleSafetyUpdate\(notice\)/);
  assert.match(src, /safetyUpdateTransition\(/);
  assert.match(
    src,
    /if \(next\.shouldEnqueue\) enqueueSettledNotice\(notice\)/
  );
  const newChat = src.slice(
    src.indexOf("function startNewChat()"),
    src.indexOf("function pickFile()", src.indexOf("function startNewChat()"))
  );
  assert.match(newChat, /safetyNoticeTokens = new Set\(\)/);
  const dismiss = src.slice(
    src.indexOf("function dismissSettledNotice()"),
    src.indexOf(
      "// The Desk already",
      src.indexOf("function dismissSettledNotice()")
    )
  );
  assert.match(
    dismiss,
    /settledNotice\.value = settledNoticeQueue\.value\[0\] \|\| null/
  );
  assert.match(
    dismiss,
    /settledNoticeQueue\.value = settledNoticeQueue\.value\.slice\(1\)/
  );
  const direct = src.slice(src.indexOf("async function resolvePending("));
  assert.match(direct, /settleSafetyUpdate\(\{ token, message: settled \}\)/);
});
