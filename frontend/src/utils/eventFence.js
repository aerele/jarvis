// Relay-Pump end-to-end epoch/seq fence (CDX-3 + CDX-12) — extracted as a PLAIN,
// importable, unit-tested module (ChatView.vue imports it). Every pump-owned realtime
// event carries (run_id/turn_id, pump_epoch, event_seq). The fence is RUN-SCOPED — ONE
// entry per run_id (NOT per message_id) — and is applied to EVERY event type regardless
// of message_id: deltas, run:start/recovering/error/end, and ALL tool events (built-in
// AND jarvis__*, which carry no message_id and previously bypassed the fence entirely —
// the CDX-3 hole).
//
// State shape: fence = { [runKey]: { epoch, seq, terminated } }
//   * epoch/seq       — the GREATEST accepted (pump_epoch, event_seq).
//   * terminated      — the highest pump_epoch at which a TERMINAL was accepted (null
//                       until the first terminal), so a repeat terminal is one-shot.
//
// We ignore any straggler from a superseded writer:
//   * a LOWER epoch than the greatest accepted (a pump that lost the lease),
//   * an equal-epoch lower/equal seq NON-terminal (a replayed/duplicate frame),
//   * ANY event below the epoch of an already-accepted terminal (a stale delta / run:start
//     / tool event that would otherwise re-open a completed reply),
//   * CDX-12: a REPEAT terminal at an already-terminated (same-or-lower) epoch (the
//     finalize backstop re-publish) — one-shot per epoch; a strictly HIGHER-epoch terminal
//     supersedes and is allowed through.
//
// CDX-12 (the P0 fix): the FIRST terminal at the current epoch legitimately shares the
// delta watermark's seq (settlement + finalize reproduce event_seq == last_event_seq == N).
// The old fence rejected that first terminal via the generic `seq <= watermark` duplicate
// check, so normal streamed/tool turns never ran the terminal UI cleanup (spinner / Stop
// state / streamingConvId stuck). A terminal is now accepted on seq EQUALITY (only a
// terminal strictly BELOW the watermark is stale); once accepted, `terminated` rejects the
// equal-identity repeat.
//
// Legacy transport events (no pump_epoch) BYPASS the fence entirely (unchanged). The run
// key is stable per turn (the pump keeps the same run_id across hops and only bumps
// pump_epoch on a takeover); turn_id === run_id (publish_fenced sets both).

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
	if (f.epoch != null && e === f.epoch && p.event_seq != null && f.seq != null) {
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
