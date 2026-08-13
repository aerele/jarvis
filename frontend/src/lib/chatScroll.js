// Chat auto-scroll invariant — extracted so the "don't drag the reader while a
// reply streams" rule is unit-testable in isolation from the (huge) ChatView SFC.
//
// Content-driven scroll (streamed text, late-loading images/charts) may follow the
// newest text to the bottom ONLY when ALL of:
//   * pinned        — the reader is parked at the bottom (a reader who scrolled up
//                     is never yanked down);
//   * NOT streaming — the turn is settled. `convStreaming` stays true across the
//                     WHOLE turn (set at send / run:start, cleared only at
//                     run:end / run:error), so it covers the gaps BETWEEN deltas;
//   * NOT revealPending — the paced reveal has drained. This OUTLIVES streaming
//                     past a late run:end, so the trailing reveal animation does
//                     not drag either.
//
// The bug this guards ("text keeps moving up"): following the bottom on every
// streamed chunk dragged the line being read up and off the top of a long answer.
// An earlier fix keyed only on `revealPending`, but the paced reveal EMPTIES
// between deltas, so each gap briefly re-enabled following and it still dragged —
// which is why `streaming` (true for the whole turn) is the load-bearing term.
export function shouldFollowBottom({ pinned, streaming, revealPending }) {
	return !!pinned && !streaming && !revealPending;
}
