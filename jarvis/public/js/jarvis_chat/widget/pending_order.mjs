// One source of truth for the parked confirmation-card order in the Desk widget.
//
// A typed "confirm 2" selects by the number the user sees: the server binds it to
// the token this client showed in that position (approval_tokens), so numbered
// cards order stably, by (created_at ascending, then token by CODE UNIT) - the
// key every client (and the server's own listing) uses. created_at (P0c) is the
// mint time; expires_at is only the fallback for a card minted before created_at
// existed (a mixed deploy).
//
// Code unit, not localeCompare: locale rules disagree on mixed-case tokens and
// would number the same cards differently from client to client.
//
// This widget is a separate build from the SPA/PWA, so the comparator is
// duplicated here and pinned by its own test (pending_order.test.mjs). The bug
// this guards against was not the comparator itself but a card built WITHOUT
// expires_at, so the tests also assert the field survives onto the item.

function sortEpoch(c) {
  return c.created_at ?? c.expires_at ?? 0;
}

export function comparePendingCards(a, b) {
  return (
    sortEpoch(a) - sortEpoch(b) ||
    (a.token < b.token ? -1 : a.token > b.token ? 1 : 0)
  );
}

export function sortPendingCards(cards) {
  return [...(cards || [])].sort(comparePendingCards);
}

// A typed "no" discards its cards before the turn starts (send_message's
// `typed_rejection`); drop them from the stack so they never linger as live
// offers. Tolerates a server that sends no such field.
export function dropDiscarded(cards, res) {
  const d = res && res.typed_rejection && res.typed_rejection.discarded;
  const gone = new Set((Array.isArray(d) ? d : []).map((x) => x && x.token));
  return gone.size
    ? (cards || []).filter((c) => !gone.has(c.token))
    : cards || [];
}

// Keep the card unless the Confirm answer settled it, with why (mirrors the SPA's
// lib/chatCardActions.js keepsChatCard); "" = the card is spent, retire it. A "no"
// without a reason_code is a legacy card's: its token is used.
const KEPT = {
  busy: "This action is being handled right now. Try again in a moment.",
  executing: "This action is already running.",
  identity_refused:
    "This action can no longer run as the user it was proposed for. Nothing ran.",
  armed_run:
    "This macro run is stopping; the action was withdrawn and nothing ran.",
};
const SETTLED_STATUS = [
  "Executed",
  "Failed",
  "Discarded",
  "Cancelled",
  "Superseded",
];
const SETTLED_CODE = ["not_found", "already_handled"];
export function keptCardMessage(res) {
  if (!res || res.ok !== false || !res.reason_code) return "";
  if (
    SETTLED_STATUS.includes(res.pa_status) ||
    SETTLED_CODE.includes(res.reason_code)
  )
    return "";
  return (
    KEPT[res.reason_code] ||
    (res.error && res.error.message) ||
    "This action could not be completed."
  );
}

// Typed yes/no scope (decision 6), mirrored from frontend/src/lib/typedCardReply.js:
// a bare or sweep phrase ("go ahead", "confirm all") binds only the cards parked
// since the user's latest message (the server's `recent`). An older card is
// "Earlier" and only its number binds it, so the hint never offers it a bare phrase.
export function isRecentCard(card) {
  return !!card && card.recent !== false;
}

export function typedApprovalHint(cards) {
  const n = (cards || []).length;
  if (!n) return "";
  const allRecent = cards.every(isRecentCard);
  if (n > 1)
    return allRecent
      ? `or type "confirm all", or "confirm 1 and ${n}"`
      : `or type "confirm 1 and ${n}"`;
  return allRecent ? 'or type "go ahead"' : 'or type "confirm 1"';
}

// An accepted send means the user just spoke: the cards on screen when they sent
// (`tokens`) are now older than their latest message.
export function markCardsEarlier(cards, tokens) {
  const shown = new Set(tokens || []);
  return (cards || []).map((c) =>
    shown.has(c.token) ? { ...c, recent: false } : c
  );
}

// A card only ever ages, so when a resync's sources disagree the Earlier reading
// wins: a transcript row loaded before the send must not re-offer a bare phrase.
export function keepEarlier(cards, ...sources) {
  const older = new Set();
  for (const src of sources)
    for (const c of src || []) if (c && c.recent === false) older.add(c.token);
  return (cards || []).map((c) =>
    older.has(c.token) && isRecentCard(c) ? { ...c, recent: false } : c
  );
}
