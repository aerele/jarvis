// The pane's and the Desk widget's one rule for a draft item: which pending items are drafts
// and what they are called. Plain node:test; the SPA's drift spec
// (frontend/src/lib/draftPending.drift.spec.js) runs the same table through isToolDraftItem.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  cardOfPendingItem,
  draftTitleOfItem,
  draftTitleProp,
  isDraftPendingItem,
} from "./draft_pending.mjs";

const draft = (over = {}) => ({
  draft: {
    verb: "create",
    doctype: "Timesheet",
    name: null,
    title: "New Timesheet",
    values: {},
    ...over,
  },
});
const viaList = (card) => ({ token: "t", preview: { card } });
const viaRow = (card) => ({ token: "t", pending_card: card });

// [name, card, expected title]. Every card is tried by the list path and the row path.
const CARDS = [
  ["a draft with its own title", draft(), "New Timesheet"],
  [
    "an update draft with its own title",
    draft({ verb: "update", name: "TS-7", title: "Timesheet TS-7" }),
    "Timesheet TS-7",
  ],
  [
    "a title that is a raw tool name falls back (create)",
    draft({ title: "create_doc" }),
    "New Timesheet",
  ],
  [
    "a title that is a prefixed tool name falls back",
    draft({ title: "jarvis__update_doc" }),
    "New Timesheet",
  ],
  [
    "a title with a tool name inside it falls back",
    draft({ title: "Run create_doc now" }),
    "New Timesheet",
  ],
  [
    "any snake_case word is a tool name",
    draft({ title: "submit_doc" }),
    "New Timesheet",
  ],
  [
    "a tool-name title falls back (update)",
    draft({ verb: "update", name: "TS-7", title: "update_doc" }),
    "Timesheet TS-7",
  ],
  ["a create without a title", draft({ title: undefined }), "New Timesheet"],
  ["a blank title", draft({ title: "   " }), "New Timesheet"],
  [
    "an update without a title",
    draft({ verb: "update", name: "TS-7", title: "" }),
    "Timesheet TS-7",
  ],
  [
    "no doctype and no title",
    { draft: { verb: "create", values: {} } },
    "New record",
  ],
  [
    "an unreadable draft (values is not an object)",
    { draft: { verb: "create", doctype: "ToDo", values: "no" } },
    "New ToDo",
  ],
  ["a draft key that is not an object", { draft: "junk" }, "Draft"],
  ["a draft key that is null", { draft: null }, "Draft"],
  ["a draft key that is an array", { draft: [] }, "Draft"],
  ["a sensitive card", { kind: "update", risk: "sensitive" }, ""],
  ["a draft that also carries a risk", { ...draft(), risk: "sensitive" }, ""],
  [
    "an ordinary gated card",
    { kind: "verb", verb: "submit", doctype: "Sales Order" },
    "",
  ],
];

for (const [name, card, title] of CARDS) {
  for (const [path, build] of [
    ["list", viaList],
    ["row", viaRow],
  ]) {
    test(`${name}: "${title}" by the ${path} path`, () => {
      assert.equal(draftTitleOfItem(build(card)), title);
      assert.equal(isDraftPendingItem(build(card)), title !== "");
    });
  }
}

// Break: a draft card never reaches the surface's "Review in full chat" because it is read from
// the wrong place (only one of the three card paths is understood).
test("the card is read from preview.card, then pending_card, else null", () => {
  const card = draft();
  assert.equal(cardOfPendingItem(viaList(card)), card);
  assert.equal(cardOfPendingItem(viaRow(card)), card);
  assert.equal(
    cardOfPendingItem({ preview: { card: "no" }, pending_card: card }),
    card
  );
  assert.equal(cardOfPendingItem({ preview: {} }), null);
  assert.equal(draftTitleOfItem({ token: "t", preview: { would: {} } }), "");
});

test("not an item: not a draft, no title", () => {
  for (const x of [null, undefined, "x", 3, []]) {
    assert.equal(isDraftPendingItem(x), false);
    assert.equal(draftTitleOfItem(x), "");
  }
});

// Break: the card's own `title` (the record's title field on an update) shown as the draft's.
test("the card's own title is never used", () => {
  const card = {
    title: "Acme Ltd",
    draft: { verb: "update", doctype: "Customer", name: "CUST-1", values: {} },
  };
  assert.equal(draftTitleOfItem(viaList(card)), "Customer CUST-1");
});

// Break: an item that is not a draft gets a key, so a surface routes it as a draft (or a flag-off
// item changes shape).
test("draftTitleProp: the key only on a draft item", () => {
  assert.deepEqual(draftTitleProp(viaList(draft())), {
    draft_title: "New Timesheet",
  });
  assert.deepEqual(draftTitleProp({ draft: { verb: "create" } }), {});
  assert.deepEqual(draftTitleProp(viaList({ kind: "create" })), {});
  assert.deepEqual(draftTitleProp(null), {});
});
