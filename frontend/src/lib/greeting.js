// The empty-chat greeting line ("Up late", "Welcome back", "Afternoon"), chosen
// from signals the SPA already holds. Pure and dependency-free so it can be unit
// tested without mounting ChatView, which is the whole reason it lives here
// rather than inline in the view.
//
// Two rules only (deliberately — the same-day and milestone signals were cut):
//   - time of day, including the late-night and early-morning edges that give the
//     empty state its charm;
//   - returning after a gap, read from the newest conversation's timestamp.
//
// Nothing leaves the browser: the gap rule reads a TIMESTAMP, never a chat title,
// and there is no backend call. Lines carry no product name, so a whitelabeled
// tenant and a Jara user both get sensible text.

// Kept mild on purpose: this is a tool people use at work, so the personality is
// a light touch at the edges of the day rather than a joke on every load.
const TIME_LINES = {
	night: ["Up late", "Burning the midnight oil", "Night owl hours"],
	early: ["Early start", "Up with the sun"],
	morning: ["Morning", "Good morning"],
	afternoon: ["Afternoon", "Good afternoon"],
	evening: ["Evening", "Good evening"],
};

const GAP_LINES = {
	long: ["Long time no see", "Welcome back"],
	short: ["Welcome back"],
};

// Days away that count as "returning". Below SHORT_GAP_DAYS the time-of-day line
// stands — someone who chatted yesterday has not been away.
const SHORT_GAP_DAYS = 3;
const LONG_GAP_DAYS = 14;

/** Bucket for a local-clock hour (0-23). Night wraps midnight, so it is checked first. */
export function timeBucket(hour) {
	const h = Number(hour);
	if (!Number.isFinite(h)) return "morning";
	if (h >= 23 || h < 5) return "night";
	if (h < 8) return "early";
	if (h < 12) return "morning";
	if (h < 17) return "afternoon";
	return "evening";
}

/** "long" | "short" | null — how long since the last real conversation. */
export function gapBucket(nowMs, lastChatAt) {
	const last = toMs(lastChatAt);
	if (!Number.isFinite(last)) return null;
	const days = (Number(nowMs) - last) / 86400000;
	// A clock skew (last chat in the "future") is not a gap.
	if (!Number.isFinite(days) || days < 0) return null;
	if (days >= LONG_GAP_DAYS) return "long";
	if (days >= SHORT_GAP_DAYS) return "short";
	return null;
}

/**
 * The greeting phrase. Rendered as `<phrase>, <firstName>`, so it never contains
 * the name itself.
 *
 * A gap WINS over the time of day: coming back after two weeks is the rarer and
 * more personal thing to acknowledge, and "Welcome back" at 2am still reads fine.
 *
 * The choice is seeded by (local day + which rule fired) rather than random, so
 * it is stable while the user sits on the screen — a line that reshuffled on every
 * re-render would read as a glitch — while still varying day to day.
 */
export function pickGreeting({ now = Date.now(), lastChatAt = null } = {}) {
	const nowMs = Number(now) || Date.now();
	const d = new Date(nowMs);
	const gap = gapBucket(nowMs, lastChatAt);
	const key = gap ? `gap:${gap}` : `time:${timeBucket(d.getHours())}`;
	const pool = gap ? GAP_LINES[gap] : TIME_LINES[timeBucket(d.getHours())];
	// Defensive: an unknown key must never render an empty greeting.
	if (!pool || !pool.length) return "Hello";
	return pool[(localDaySeed(d) + hash(key)) % pool.length];
}

// A stable day number in LOCAL time. Not a UTC day count: the greeting follows
// the user's clock, so the day has to turn over at their midnight.
function localDaySeed(d) {
	return d.getFullYear() * 400 + (d.getMonth() + 1) * 31 + d.getDate();
}

function hash(s) {
	let h = 0;
	for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
	return h;
}

// Frappe hands back naive "YYYY-MM-DD HH:MM:SS" strings in the site's timezone.
// The space form is not valid ISO, so normalise it before parsing; day-granularity
// is all the gap rule needs, which is why no timezone maths is attempted here.
function toMs(v) {
	if (v == null || v === "") return NaN;
	if (typeof v === "number") return v;
	if (v instanceof Date) return v.getTime();
	return Date.parse(String(v).trim().replace(" ", "T"));
}
