// Quiet-period detector for a streaming reply. The terminal run:end only lands
// after the server settles the turn (a couple of seconds after the last token),
// so the copy bar and the reply's time used to appear that much late. Each
// streamed delta re-arms a short timer; when it fires with no further delta the
// text is treated as finished and `onSettle(id, lastDeltaMs)` is called with
// the client time of that last delta. A later delta (answer resumed) calls
// `onResume(id)` so the bar never stays up on text that is still growing.
export function createReplySettle({ onSettle, onResume, quietMs = 600 }) {
	const timers = new Map();
	const settled = new Set();
	return {
		touch(id, now = Date.now()) {
			clearTimeout(timers.get(id));
			if (settled.delete(id)) onResume?.(id);
			timers.set(
				id,
				setTimeout(() => {
					timers.delete(id);
					settled.add(id);
					onSettle(id, now);
				}, quietMs)
			);
		},
		clear(id) {
			clearTimeout(timers.get(id));
			timers.delete(id);
			settled.delete(id);
		},
	};
}
