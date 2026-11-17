// The widget's pending items: all three builders (realtime frame, transcript row, pending list)
// give a draft card its title. The panel keeps the FIRST item it sees for a token (the row path
// before the list), so a builder that forgot the title would show a raw tool name with a Confirm.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { applyEvent, emptyStream } from "./chat_stream.mjs";
import {
  listPendingItem,
  mergePendingByToken,
  rowPendingItem,
} from "./pending_items.mjs";

const toEpoch = (s) =>
  s ? Date.parse(String(s).replace(" ", "T")) / 1000 : null;
const DRAFT = {
  kind: "create",
  draft: {
    verb: "create",
    doctype: "Timesheet",
    title: "New Timesheet",
    values: {},
  },
};
const UPDATE = {
  kind: "update",
  title: "Acme Ltd",
  draft: {
    verb: "update",
    doctype: "Customer",
    name: "CUST-1",
    title: "update_doc",
    values: {},
  },
};

// The same parked card as each of the three sources sends it.
function threeWays(token, card, tool = "create_doc") {
  const frame = {
    kind: "action:pending",
    token,
    pending_card_safe: true,
    tool,
    summary: `${tool} doctype=X`,
    preview: { card },
  };
  return {
    live: applyEvent(emptyStream(), frame).pending[0],
    row: rowPendingItem(
      {
        role: "tool",
        tool_status: "pending",
        tool_call_id: token,
        tool_name: tool,
        pending_card: card,
        pending_card_safe: true,
        creation: "2026-11-17 10:00:00",
        recent: true,
      },
      toEpoch
    ),
    list: listPendingItem({
      token,
      tool,
      summary: frame.summary,
      preview: { card },
    }),
  };
}

// Break: one builder forgets the title; whichever the merge meets first for that token would put
// "create_doc" (or the server's describe line) back on screen with a Confirm.
test("all three builders give a draft its title, as draft_title and summary", () => {
  for (const [card, title] of [
    [DRAFT, "New Timesheet"],
    [UPDATE, "Customer CUST-1"],
  ]) {
    const w = threeWays("t1", card);
    for (const [name, item] of Object.entries(w)) {
      assert.equal(item.draft_title, title, name);
      assert.equal(item.summary, title, name);
      assert.doesNotMatch(`${item.summary} ${item.draft_title}`, /_doc/, name);
      // the item never carries the card itself
      assert.equal(item.preview, undefined, name);
      assert.equal(item.card, undefined, name);
      assert.equal(item.pending_card, undefined, name);
    }
  }
});

// Break: an item that is not a draft changes shape (a new key, a changed summary), so a flag-off
// card or a sensitive card is routed as a draft, or loses its Confirm.
test("an item that is not a draft keeps exactly the keys and summary it had", () => {
  for (const card of [{ kind: "create" }, { risk: "sensitive" }, null]) {
    const w = threeWays("t2", card);
    assert.equal("draft_title" in w.live, false);
    assert.deepEqual(Object.keys(w.row), [
      "token",
      "tool",
      "summary",
      "created_at",
      "expires_at",
      "approve_run",
      "recent",
      "pending_card_safe",
    ]);
    assert.equal(w.row.summary, "create_doc");
    assert.deepEqual(Object.keys(w.list), Object.keys(w.row));
    assert.equal(w.list.summary, "create_doc doctype=X");
  }
});

// The panel itself has no mount test, so pin its wiring. Break: a draft item gets Confirm back
// (the button's v-if loses draft_title), another control starts resolvePending, the typed hint
// returns for a stack with a draft, or a builder is inlined back into Panel.vue without the title.
const panel = fs.readFileSync(
  fileURLToPath(new URL("./Panel.vue", import.meta.url)),
  "utf8"
);
const squash = (t) => t.replace(/\s+/g, " ").trim();

test("Panel.vue: a draft item has no Confirm, one solid Review in full chat, and no typed hint", () => {
  const row = panel.slice(panel.indexOf('v-for="(p, pi) in orderedPending"'));
  const acts = squash(
    row.slice(
      row.indexOf('class="jvp-pending-acts"'),
      row.indexOf("jvp-typehint")
    )
  );
  assert.match(acts, /v-if="!p\.approve_run && !p\.draft_title"/);
  assert.match(
    acts,
    /p\.approve_run \|\| p\.draft_title \? 'jvp-btn-solid' : 'jvp-btn-subtle'/
  );
  assert.equal(
    acts.split("@click=").length - 1,
    2,
    "exactly two buttons act: open-full and Confirm"
  );
  assert.match(
    squash(row),
    /<div v-if="typedApprovalHint" class="jvp-typehint">/
  );
  assert.match(panel, /visibleTypedHint as hintFor/);
  // Confirm is the ONLY thing that calls resolvePending.
  assert.equal(
    panel.split("resolvePending(").length - 1,
    2,
    "the Confirm button and the definition"
  );
  assert.match(panel, /rowPendingItem\(m, toEpoch\)/);
  assert.match(panel, /\.map\(listPendingItem\)/);
  // the merge (mergePendingByToken, tested below) is what the panel uses, with these builders
  assert.match(panel, /mergePendingByToken\(base, rowItems, backstop\)/);
  // a draft's line is the shared one ("Draft: <title>", "Draft to review" for the bare fallback)
  assert.match(squash(panel), /\? draftLine\(p\.draft_title\)/);
});

// Break: the merge keeps a later source's item for a token (the list's, without the row's title or
// recency), drops an item for a second token, reorders cards (their typed numbers move), or keeps a
// token-less item. Behaviour, not a source pin.
test("mergePendingByToken: the first item per token wins, in first-seen order, token-less dropped", () => {
  const live = { token: "a", from: "live" };
  const rowA = { token: "a", from: "row" };
  const rowB = { token: "b", from: "row" };
  const listB = { token: "b", from: "list" };
  const listC = { token: "c", from: "list" };
  assert.deepEqual(
    mergePendingByToken(
      [live],
      [rowA, rowB],
      [listB, listC, { from: "no token" }]
    ),
    [live, rowB, listC]
  );
  // a failed list read: the live stack is the base; with none, rows then list
  assert.deepEqual(mergePendingByToken([], [rowB], [listB]), [rowB]);
  assert.deepEqual(mergePendingByToken([], [], []), []);
  // a draft that came from every source reads the same whichever wins
  const w = threeWays("d1", DRAFT);
  for (const order of [
    [[w.live], [w.row], [w.list]],
    [[], [w.row], [w.list]],
    [[], [], [w.list]],
  ])
    assert.equal(mergePendingByToken(...order)[0].draft_title, "New Timesheet");
});
