// Client-side throttle for the post-reply feedback line. It decides whether the
// "Was this reply helpful?" bar appears after a reply, and keeps the ask RARE so
// it never nags. All state lives in localStorage, keyed per day, so no server
// round-trip is needed to decide - and a reload / new day resets the caps.
//
// The rules (see the design plan): a reply must be genuinely long (> 1 min),
// then only ~1 in 3 of those show it, with a cooldown between asks, a hard cap
// per session, and it stops entirely once the user rates or ignores it twice.

const KEY = "jvChatFeedback.v1";

// The one-minute floor is the single knob to dial if it still feels too frequent
// (raise to 90s / 120s). Shared with the mini widget.
export const FEEDBACK_MIN_MS = 60000;

const SHOW_PROB = 1 / 3; // among qualifying replies
const COOLDOWN_REPLIES = 5; // replies between asks
const SESSION_CAP = 2; // hard cap per day
const IGNORE_CAP = 2; // stop after this many shown-and-ignored

function today() {
	return new Date().toISOString().slice(0, 10);
}

function fresh() {
	return {
		day: today(),
		n: 0,
		lastShown: null,
		shown: 0,
		rated: false,
		ignores: 0,
		stopped: false,
	};
}

function load() {
	try {
		const raw = JSON.parse(localStorage.getItem(KEY) || "null");
		if (!raw || raw.day !== today()) return fresh();
		return {
			day: raw.day,
			n: raw.n | 0,
			lastShown: raw.lastShown == null ? null : raw.lastShown | 0,
			shown: raw.shown | 0,
			rated: !!raw.rated,
			ignores: raw.ignores | 0,
			stopped: !!raw.stopped,
		};
	} catch {
		return fresh();
	}
}

function save(s) {
	try {
		localStorage.setItem(KEY, JSON.stringify(s));
	} catch {
		// private mode / storage disabled - degrade to "never show", never throw.
	}
}

// Call once per completed reply. Advances the per-day reply counter and returns
// true iff this reply should show the feedback bar. Mutates + persists state, so
// the caller MUST guard against evaluating the same reply twice.
export function shouldOfferFeedback(durationMs) {
	const s = load();
	s.n += 1;
	let show = false;
	const cooldownOk = s.lastShown === null || s.n - s.lastShown >= COOLDOWN_REPLIES;
	if (
		!s.stopped &&
		!s.rated &&
		s.shown < SESSION_CAP &&
		Number(durationMs) >= FEEDBACK_MIN_MS &&
		cooldownOk &&
		Math.random() < SHOW_PROB
	) {
		show = true;
		s.shown += 1;
		s.lastShown = s.n;
	}
	save(s);
	return show;
}

// The user gave a rating (up or down): stop asking for the rest of the session.
export function markRated() {
	const s = load();
	s.rated = true;
	save(s);
}

// The bar was shown and dismissed without a rating (moved on / typed next query).
// Back off entirely after IGNORE_CAP of these.
export function markIgnored() {
	const s = load();
	s.ignores += 1;
	if (s.ignores >= IGNORE_CAP) s.stopped = true;
	save(s);
}
