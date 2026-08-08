// SUX-7 / jarvis#681: the "Finishing…" affordance's bookkeeping, extracted as a
// PLAIN, importable, unit-tested module (ChatView.vue imports it).
//
// What the affordance is. Settlement publishes the turn's terminal `run:end` with
// `enrichment_pending`, which tells the SPA to keep a subtle "Finishing…" line under
// an otherwise finished reply while the finalize job is still adding late enrichment
// (attachments, canvas, auto-title). `message:enriched` clears it.
//
// The defect this module closes (jarvis#681, e2e finding F26). `message:enriched` is
// a single best-effort realtime push at the end of a chain of best-effort enrichment
// effects, and the affordance had NOTHING else that could ever clear it. So all of
// these left it up permanently on a reply that was already complete and correct:
//   * a finalize effect that keeps failing. The usage poll retries BY DESIGN when the
//     gateway session row is missing or stale, which is exactly what a live credential
//     apply produces, and the turn then sits in `finalizing` until the 5-minute pump
//     watchdog has burned the 3-attempt budget. That is 15 minutes at best, and
//     forever on any bench whose scheduler is not running.
//   * the push itself lost: a socket blip, a hidden tab, a client that was on another
//     route when it fired. There is no replay on the socket.
//   * a reload. `loadConversation` replaces the messages but never the pending set,
//     so navigating away and back re-rendered the same stuck line.
//
// A permanent "Finishing…" asserts the answer is incomplete when it is not, which is
// strictly worse than saying nothing. So the affordance is BOUNDED: every entry
// carries a deadline, and on expiry it is dropped and the owner is told, so it can
// refetch the conversation once and still pull in whatever enrichment did land.
//
// The bound is deliberately generous. Enrichment normally completes within a couple
// of seconds; the slowest healthy path is the usage effect's bounded gateway poll
// (three reads with 1.5s sleeps) plus short-queue latency. Two minutes is far past
// any healthy run, and short enough that nobody reads the line as permanent.
export const ENRICHMENT_PENDING_MAX_MS = 120000;

const noop = () => {};

/**
 * Track which assistant messages are still awaiting `message:enriched`, with a
 * deadline on each so the affordance can never outlive its usefulness.
 *
 * The tracker owns the timers; the caller owns the rendering. `onChange` is handed a
 * fresh Set on every mutation (so a Vue ref assignment triggers a re-render, matching
 * how ChatView already replaced the Set rather than mutating it), and `onExpire` is
 * called once per message that timed out, AFTER `onChange`, so the owner sees the
 * cleared state before it decides to resync.
 *
 * `setTimer` / `clearTimer` are injectable purely so the deadline behaviour is
 * testable without real time passing.
 */
export function createEnrichmentPending(options = {}) {
	const maxMs = options.maxMs != null ? options.maxMs : ENRICHMENT_PENDING_MAX_MS;
	const onChange = options.onChange || noop;
	const onExpire = options.onExpire || noop;
	const setTimer = options.setTimer || ((fn, ms) => setTimeout(fn, ms));
	const clearTimer = options.clearTimer || ((handle) => clearTimeout(handle));

	const timers = new Map(); // messageId -> timer handle
	let ids = new Set(); // messageIds currently showing "Finishing…"

	function emit() {
		onChange(new Set(ids));
	}

	function cancelTimer(messageId) {
		if (!timers.has(messageId)) return;
		clearTimer(timers.get(messageId));
		timers.delete(messageId);
	}

	function expire(messageId) {
		timers.delete(messageId);
		if (!ids.delete(messageId)) return; // already cleared by message:enriched
		emit();
		onExpire(messageId);
	}

	return {
		/**
		 * A terminal `run:end` said enrichment is still owed for this reply. Idempotent
		 * and deliberately NON-extending: a re-delivered terminal (the CDX-12 finalize
		 * backstop re-publishes one) must not push the deadline out, or a server that
		 * keeps re-announcing the same stuck turn would keep the line up indefinitely,
		 * which is the very failure being fixed. The clock runs from the FIRST terminal.
		 */
		mark(messageId) {
			if (!messageId || ids.has(messageId)) return false;
			ids.add(messageId);
			timers.set(
				messageId,
				setTimer(() => expire(messageId), maxMs)
			);
			emit();
			return true;
		},

		/** `message:enriched` landed (or the caller otherwise knows enrichment settled). */
		clear(messageId) {
			if (!messageId || !ids.has(messageId)) return false;
			cancelTimer(messageId);
			ids.delete(messageId);
			emit();
			return true;
		},

		/** Drop everything and cancel every timer (component teardown). */
		reset() {
			for (const messageId of [...timers.keys()]) cancelTimer(messageId);
			if (!ids.size) return;
			ids = new Set();
			emit();
		},

		has(messageId) {
			return ids.has(messageId);
		},

		get size() {
			return ids.size;
		},
	};
}
