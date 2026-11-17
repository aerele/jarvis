// The visible, cancellable countdown before a chat hand-off opens its page, as a
// plain state machine. ChatView wires it (card, Esc, send, visibility); the clock
// and timers are injected so every deadline is unit-testable.
//
// Deadline-based, not tick-counting: a throttled background tab cannot make it
// late or early. Pausing freezes the remaining time; resuming never leaves less
// than RESUME_FLOOR_MS (a pointer or focus that arrives with 200 ms left must not
// starve the user of time to read). One interval, ticking every TICK_MS.
//
// A tick that arrives more than STALL_MS after the previous one means the clock leapt
// (a suspended laptop, a frozen tab). It counts as a resume, never as an expiry, so a
// page the user never saw counting down does not open on wake-up.
//
// cancel(reason) is free text for the exit callback ("stay", "esc", "typing", "send",
// "switch", "unmount", "claimed" (another tab took the build), ...); nothing here branches on it.
//
// Every exit (cancel, expiry, open now) clears the instance BEFORE it calls out, so
// state() is null inside onExpire / onOpenNow / onCancel and a callback may start
// the next countdown. A message id is "handled" from its first start, whatever the
// exit: a replayed run:end can never start a second countdown for it.

export const GOTO_COUNTDOWN_MS = 3000;
export const RESUME_FLOOR_MS = 2000;
// Why a countdown is paused (GotoCard reports hover and focus; ChatView reports a hidden tab).
export const PAUSE = { HOVER: "hover", FOCUS: "focus", HIDDEN: "hidden" };
const TICK_MS = 250;
const STALL_MS = 1000;
const HANDLED_MAX = 200;

// Per page load: survives a ChatView remount, bounded so a long session cannot grow it.
const moduleHandled = new Set();

const noop = () => {};

/**
 * @param {{
 *   durationMs?: number,
 *   now: () => number,
 *   setInterval: (fn: () => void, ms: number) => any,
 *   clearInterval: (id: any) => void,
 *   handled?: Set<string>,
 *   onTick?: (state: object) => void,
 *   onExpire?: (state: object) => void,
 *   onOpenNow?: (state: object) => void,
 *   onCancel?: (state: object, reason: string) => void,
 * }} deps
 */
export function createGotoCountdown(deps) {
	const {
		durationMs = GOTO_COUNTDOWN_MS,
		now,
		setInterval: startInterval,
		clearInterval: stopInterval,
		handled = moduleHandled,
		onTick = noop,
		onExpire = noop,
		onOpenNow = noop,
		onCancel = noop,
	} = deps;

	const reasons = new Set();
	// active: { messageId, runId, page, action, deadline, frozenMs, lastTick, shown, timer }
	let active = null;

	function markHandled(messageId) {
		handled.delete(messageId); // re-adding refreshes its age
		handled.add(messageId);
		while (handled.size > HANDLED_MAX) handled.delete(handled.values().next().value);
	}

	function remainingMs() {
		return reasons.size ? active.frozenMs : active.deadline - now();
	}

	function snapshot() {
		const { messageId, runId, page, action } = active;
		const secondsLeft = Math.max(1, Math.ceil(remainingMs() / 1000));
		return { messageId, runId, page, action, secondsLeft, paused: reasons.size > 0 };
	}

	// Notify when what the card shows (seconds, paused) differs from what it last got.
	function publish() {
		const s = snapshot();
		if (active.shown === `${s.secondsLeft}/${s.paused}`) return;
		active.shown = `${s.secondsLeft}/${s.paused}`;
		onTick(s);
	}

	// Leave: clear the instance first, hand back the last snapshot.
	function leave() {
		const last = snapshot();
		stopInterval(active.timer);
		active = null;
		reasons.clear();
		return last;
	}

	function tick() {
		if (!active || reasons.size) return;
		const t = now();
		if (t - active.lastTick > STALL_MS) {
			// What the user saw at the last tick is what they still get (at least the floor).
			active.deadline = t + Math.max(active.deadline - active.lastTick, RESUME_FLOOR_MS);
			active.lastTick = t;
			publish();
			return;
		}
		active.lastTick = t;
		if (remainingMs() <= 0) onExpire(leave());
		else publish();
	}

	function start({ messageId, runId, page, action }) {
		if (handled.has(messageId)) return false;
		if (active) cancel("switch");
		// A cancel callback may itself have started a countdown: do not overwrite it
		// (that would leak its interval).
		if (active) return false;
		markHandled(messageId);
		reasons.clear(); // defensive: leave() already cleared them
		active = {
			messageId,
			runId,
			page,
			action,
			deadline: now() + durationMs,
			frozenMs: durationMs,
			lastTick: now(),
			shown: "",
			timer: null,
		};
		active.timer = startInterval(tick, TICK_MS);
		publish();
		return true;
	}

	function pause(reason) {
		if (!active) return;
		const wasRunning = reasons.size === 0;
		reasons.add(reason);
		if (wasRunning) {
			active.frozenMs = Math.max(0, active.deadline - now());
			publish();
		}
	}

	function resume(reason) {
		if (!active || !reasons.has(reason)) return;
		reasons.delete(reason);
		if (reasons.size) return;
		active.deadline = now() + Math.max(active.frozenMs, RESUME_FLOOR_MS);
		active.lastTick = now();
		publish();
	}

	function cancel(reason) {
		if (!active) return;
		onCancel(leave(), reason);
	}

	function openNow() {
		if (!active) return;
		onOpenNow(leave());
	}

	return {
		start,
		pause,
		resume,
		cancel,
		openNow,
		state: () => (active ? snapshot() : null),
		isHandled: (messageId) => handled.has(messageId),
		markHandled,
	};
}
