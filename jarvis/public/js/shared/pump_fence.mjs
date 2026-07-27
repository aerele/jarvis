// The Relay-Pump epoch/seq fence, as a SHARED CONSUMER CONTRACT (JF-018).
//
// Every pump-owned realtime frame carries (run_id/turn_id, pump_epoch, event_seq) —
// see jarvis/chat/turn_state.py publish_fenced(). Without a fence, a superseded
// pump's late CUMULATIVE delta overwrites a newer projection after a handoff/replay,
// and a stale terminal re-opens (or re-closes) a settled turn.
//
// The desktop SPA has fenced since CDX-3/CDX-12; the PWA and the Desk widget did not,
// because the logic was written inside desktop's ChatView instead of a module all
// three surfaces could consume. This file is that module, placed under
// jarvis/public/js/ because the Desk widget is an esbuild bundle that cannot reach
// frontend/src or the "@/" alias (see jarvis_chat/widget/panel_readiness.mjs for the
// same constraint). .mjs so it is unambiguously ESM regardless of the nearest
// package.json "type".
//
// MIRRORED SEMANTICS — this is a copy, not a variation. fenceKey/fenceReject/
// fenceAccept below reproduce frontend/src/utils/eventFence.js (lines 35-79 at the
// JF-018 snapshot) EXACTLY, which is itself the extraction of desktop's fence in
// frontend/src/views/ChatView.vue (pumpFenceReject/pumpFenceAccept, lines 4006-4012,
// applied at lines 7156-7157, 7174-7175, 7223-7224, 7241-7242, 7254-7255,
// 7279-7280 and 7380-7381 at that snapshot). Desktop can adopt this module verbatim
// when JF-013 refactors ChatView; jarvis/tests/test_pump_fence_shared_client.py runs
// a decision-parity walk against the desktop copy so the two cannot silently drift.
//
// State shape: fence = { [runKey]: { epoch, seq, terminated } }
//   * epoch/seq  — the GREATEST accepted (pump_epoch, event_seq).
//   * terminated — the highest pump_epoch at which a TERMINAL was accepted (null until
//                  the first terminal), so a repeat terminal is one-shot.
//
// Dropped frames have NO user-visible effect: every terminal path on these surfaces
// re-fetches the durable conversation over HTTP, which is what converges content.

// The two kinds desktop treats as TERMINAL (ChatView.vue 7279 / 7380 pass isTerminal
// = true and nothing else does). A terminal latches `terminated` for its epoch.
export const TERMINAL_KINDS = new Set(["run:end", "run:error"]);

// The kinds desktop puts THROUGH the fence. Everything else (run:status,
// queue:position, turn:cancelled, action:confirmed, action:pending, action:resolved,
// canvas, conversation:renamed, message:enriched, …) bypasses it there and must
// bypass it here — those frames are not pump-sequenced projections of the reply.
export const FENCED_KINDS = new Set([
  "run:start",
  "run:recovering",
  "assistant:delta",
  "tool:start",
  "tool:end",
  "run:end",
  "run:error",
]);

export function createFence() {
  return {};
}

export function fenceKey(p) {
  return p.run_id || p.turn_id || null;
}

export function fenceReject(fence, p, isTerminal) {
  if (p.pump_epoch == null) return false; // legacy / non-pump -> accept
  const k = fenceKey(p);
  if (!k) return false;
  const f = fence[k];
  if (!f) return false;
  const e = p.pump_epoch;
  if (f.terminated != null && e < f.terminated) return true; // any event below a higher-epoch terminal
  // CDX-12: a repeat terminal at an already-terminated (same-or-lower) epoch is one-shot.
  if (isTerminal && f.terminated != null && e <= f.terminated) return true;
  if (f.epoch != null && e < f.epoch) return true; // superseded writer
  if (
    f.epoch != null &&
    e === f.epoch &&
    p.event_seq != null &&
    f.seq != null
  ) {
    if (isTerminal) {
      // CDX-12: reaching here as a terminal means it is the FIRST terminal at the
      // current epoch (a repeat was rejected above via `terminated`; a lower-epoch one
      // via the superseded-writer check). It legitimately shares the delta watermark's
      // seq, so accept equality — only a terminal STRICTLY BELOW the watermark is stale.
      if (p.event_seq < f.seq) return true;
    } else if (p.event_seq <= f.seq) {
      return true; // duplicate/older NON-terminal frame at the same epoch
    }
  }
  return false;
}

export function fenceAccept(fence, p, isTerminal) {
  if (p.pump_epoch == null) return; // legacy / non-pump -> nothing to track
  const k = fenceKey(p);
  if (!k) return;
  const prev = fence[k] || { epoch: null, seq: null, terminated: null };
  let { epoch, seq, terminated } = prev;
  const e = p.pump_epoch;
  if (epoch == null || e > epoch) {
    epoch = e;
    seq = p.event_seq != null ? p.event_seq : null; // reset the seq watermark on a new (higher) epoch
  } else if (e === epoch && p.event_seq != null) {
    seq = seq == null ? p.event_seq : Math.max(seq, p.event_seq);
  }
  if (isTerminal) terminated = terminated == null ? e : Math.max(terminated, e);
  fence[k] = { epoch, seq, terminated };
}

// The whole contract in one call, so a consumer cannot forget the accept half (the
// bug that check-only fences ship with): returns TRUE to apply the frame, FALSE to
// drop it. Kinds outside FENCED_KINDS are applied WITHOUT touching fence state —
// identical to desktop, where those cases simply have no fence call.
export function admitEvent(fence, payload) {
  const p = payload || {};
  if (!FENCED_KINDS.has(p.kind)) return true;
  const isTerminal = TERMINAL_KINDS.has(p.kind);
  if (fenceReject(fence, p, isTerminal)) return false;
  fenceAccept(fence, p, isTerminal);
  return true;
}
