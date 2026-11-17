// Shared by the dashboard chat pane and the Desk widget: is a pending confirmation a SERVER
// DRAFT CARD (the tool path), and what is its human title. Pure, no imports, so the SPA's vite
// build and the widget's esbuild bundle both take it.
//
// Those two surfaces cannot draw a draft's contents, so a draft there shows its title and one
// "Review in full chat" button and nothing that approves it unseen.
//
// "Is a draft item" is the same rule as frontend/src/lib/toolDraft.js `isToolDraftItem` (the
// full chat's): the card has a `draft` key and no truthy `risk`. A spec runs both over one
// table so they cannot drift. An unreadable draft (`draft` present but not an object) still
// counts, so it goes to "Review in full chat" and never to a blind Confirm.

const isObject = (v) => !!v && typeof v === "object" && !Array.isArray(v);

// The card of a pending item, from the paths the surfaces have: a pending-list item or realtime
// push (`item.preview.card`), or a tool row of `get_conversation` (`item.pending_card`). null
// when there is none.
export function cardOfPendingItem(item) {
  if (!isObject(item)) return null;
  if (isObject(item.preview) && isObject(item.preview.card))
    return item.preview.card;
  return isObject(item.pending_card) ? item.pending_card : null;
}

// True when the item's card has a `draft` key and no `risk`. A sensitive card (`risk`) and every
// other gated card keep their existing confirmation.
export function isDraftPendingItem(item) {
  const card = cardOfPendingItem(item);
  return (
    !!card && Object.prototype.hasOwnProperty.call(card, "draft") && !card.risk
  );
}

const text = (v) => {
  if (typeof v === "string") return v.trim();
  return typeof v === "number" && Number.isFinite(v) ? String(v) : "";
};

// A raw tool name: `create_doc`, `update_doc`, `jarvis__create_doc`, any snake_case word.
const TOOL_NAME =
  /\b(?:create|update)_doc\b|^[A-Za-z0-9]+(?:_+[A-Za-z0-9]+)+$/i;

// What a pending item is called on a surface that cannot draw it. "" for an item that is not a
// draft item. For a draft item: `card.draft.title`, unless it is empty or a raw tool name, then
// "New <doctype>" (create) or "<doctype> <name>" (update). `draft` that is not an object (an
// unreadable draft) gives the generic "Draft". Never the card's own `title` (on an update that
// is the record's title field) and never a tool name.
export function draftTitleOfItem(item) {
  if (!isDraftPendingItem(item)) return "";
  const draft = cardOfPendingItem(item).draft;
  if (!isObject(draft)) return "Draft";
  const given = text(draft.title);
  if (given && !TOOL_NAME.test(given)) return given;
  const doctype = text(draft.doctype) || "record";
  return draft.verb === "update"
    ? `${doctype} ${text(draft.name)}`.trim()
    : `New ${doctype}`;
}

// `{draft_title}` for a pending item that is a draft, `{}` for every other, so an item that is
// not a draft keeps exactly the keys it had before (spread it into the item).
export function draftTitleProp(item) {
  const title = draftTitleOfItem(item);
  return title ? { draft_title: title } : {};
}
