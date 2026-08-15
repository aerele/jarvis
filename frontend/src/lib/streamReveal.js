/**
 * Paces streamed assistant text onto the screen at a steady rate.
 *
 * The server publishes the CUMULATIVE reply on every `assistant:delta`, and the
 * view used to assign it straight through. That makes the reply move at exactly
 * the rate the model and the socket happen to deliver it: a few characters, then
 * a stall, then a whole paragraph in one frame. The content is correct and the
 * motion is not, which is what reads as sloppy.
 *
 * This holds the newest text as a TARGET and walks a reveal cursor toward it a
 * little each frame, faster when it is further behind, so a burst is absorbed
 * instead of dumped. Nothing is ever dropped or reordered: the cursor only moves
 * forward, and every snap path below ends with the full target on screen.
 *
 * Snap (skip the animation, show everything now) is required, not optional, in
 * four cases:
 *
 *   1. FIRST delta of a message. Starting from zero would leave the assistant row
 *      empty for a frame, and the view hides an empty streaming row, so the reply
 *      would flicker out of existence just as it arrives.
 *   2. REWRITE. A recovered run republishes text that is not an extension of what
 *      was shown. Animating a rewrite would play the difference as a typo.
 *   3. TERMINAL (run end, error, stop, conversation switch). The final text is
 *      authoritative the moment it lands; a cursor still mid-walk would truncate
 *      the answer.
 *   4. HIDDEN TAB. requestAnimationFrame stops while the tab is in the
 *      background, so an animating message would freeze there indefinitely.
 *
 * The caller owns the frame loop and the DOM; this module owns only the cursor
 * arithmetic and the snap rules, so both are testable without mounting the view.
 */

/** Never crawl slower than this, or a long tail takes visibly too long. */
export const MIN_STEP = 2;
/** Never move more than this per frame, or a burst is a jump cut again. */
export const MAX_STEP = 180;
/** Frames-ish to absorb a backlog. Lower is snappier, higher is smoother. */
export const CATCH_UP = 7;

/**
 * Where the cursor lands next frame.
 * @param {number} shown characters currently on screen
 * @param {number} targetLen characters available
 * @returns {number} the new cursor position, never past targetLen
 */
export function nextRevealed(shown, targetLen) {
	if (shown >= targetLen) return targetLen;
	const backlog = targetLen - shown;
	const step = Math.min(MAX_STEP, Math.max(MIN_STEP, Math.ceil(backlog / CATCH_UP)));
	return Math.min(targetLen, shown + step);
}

/**
 * A per-conversation revealer. Keyed by message id, so two messages streaming at
 * once (a recovery landing beside a live turn) each keep their own cursor.
 */
export function createRevealer() {
	const states = new Map();

	return {
		/**
		 * Take a cumulative delta. Returns the text to show RIGHT NOW, which is
		 * the full text on a first delta or a rewrite and a prefix otherwise.
		 */
		receive(id, text) {
			const next = text || "";
			const st = states.get(id);
			if (!st) {
				states.set(id, { target: next, shown: next.length });
				return next;
			}
			// Not an extension of what we were revealing: the reply was rewritten,
			// so there is no animation to continue.
			if (!next.startsWith(st.target)) {
				st.target = next;
				st.shown = next.length;
				return next;
			}
			st.target = next;
			return st.target.slice(0, st.shown);
		},

		/**
		 * Advance the cursor. Returns null when this id has nothing left to reveal.
		 *
		 * ``frames`` is how many frames' worth of catch-up to apply in one go. It
		 * exists because each returned text costs the caller a FULL markdown re-parse
		 * and a v-html rewrite of the whole message, so painting on every one of 60
		 * frames a second is many times the work the old socket-rate rendering did,
		 * and a long reply starts dropping frames. The caller paints less often and
		 * passes the frames it skipped, which keeps the reveal SPEED identical while
		 * cutting the render cost. Same arithmetic, applied N times, so pacing stays
		 * exactly what the tests pin.
		 */
		tick(id, frames = 1) {
			const st = states.get(id);
			if (!st) return null;
			if (st.shown >= st.target.length) return null;
			for (let i = 0; i < Math.max(1, frames) && st.shown < st.target.length; i++)
				st.shown = nextRevealed(st.shown, st.target.length);
			return { text: st.target.slice(0, st.shown), done: st.shown >= st.target.length };
		},

		/** Ids still mid-reveal, so the caller knows whether to keep the loop alive. */
		pending() {
			const out = [];
			for (const [id, st] of states) if (st.shown < st.target.length) out.push(id);
			return out;
		},

		/** Snap one message to its full text and forget it. Returns that text. */
		flush(id) {
			const st = states.get(id);
			if (!st) return null;
			states.delete(id);
			return st.target;
		},

		/** Snap everything. Returns [id, fullText] pairs for the caller to apply. */
		flushAll() {
			const out = [];
			for (const [id, st] of states) out.push([id, st.target]);
			states.clear();
			return out;
		},

		/** Forget a message without applying anything (its row is gone). */
		drop(id) {
			states.delete(id);
		},

		get size() {
			return states.size;
		},
	};
}
