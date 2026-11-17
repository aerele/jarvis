// The pending-confirmation items of the Desk widget, built from the three sources it merges
// (the transcript's pending tool rows, the pending list, the realtime push). Kept out of Panel.vue
// so each can be tested without a browser.
//
// A server DRAFT card (flag jarvis_draft_cards) is not confirmed from this panel: the widget cannot
// show what it holds. So EVERY builder gives such an item its human title as `draft_title` (and as
// `summary`, so a tool name can never be what the item says). The panel then shows "Draft: <title>"
// and "Review in full chat" and no Confirm. All three builders must do it: the merge keeps the
// first item it sees for a token, so one builder that forgets would win with a raw tool name.
// An item that is not a draft has exactly the keys it had before drafts.
import { pendingApproveRun, withDraftTitle } from "./chat_stream.mjs";

// A pending tool row of get_conversation. `toEpoch` turns a Frappe timestamp into epoch seconds.
export function rowPendingItem(m, toEpoch) {
  return withDraftTitle(
    {
      token: m.tool_call_id,
      tool: m.tool_name || "",
      summary: m.tool_name || "",
      // The row has no dedicated created_at field; its own creation
      // timestamp is stamped in the same request as the mint and is close
      // enough for ordering (the comparator falls back to expires_at
      // anyway when this is missing).
      created_at: toEpoch(m.creation),
      expires_at: toEpoch(m.expires_at),
      approve_run: !!(m.pending_card && m.pending_card.approve_run),
      recent: m.recent !== false,
      pending_card_safe: true,
    },
    m
  );
}

// An item of the pending list (list_pending_confirmations), the Redis backstop.
export function listPendingItem(r) {
  return withDraftTitle(
    {
      token: r.token,
      tool: r.tool || "",
      summary: r.summary || r.preview || "",
      created_at: r.created_at ?? null,
      expires_at: r.expires_at ?? null,
      approve_run: pendingApproveRun(r.preview),
      recent: r.recent !== false,
      pending_card_safe: true,
    },
    r
  );
}
