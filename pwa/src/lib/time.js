// Frappe hands back naive datetimes in the site timezone ("2026-07-13 14:02:11").
// Safari refuses to parse that form, so normalise before constructing a Date --
// otherwise every timestamp on iOS reads "Invalid Date".
export function parseTs(value) {
	if (!value) return NaN;
	const t = new Date(String(value).replace(" ", "T")).getTime();
	return Number.isNaN(t) ? NaN : t;
}

export function relativeTime(value) {
	const then = parseTs(value);
	if (Number.isNaN(then)) return "";

	const secs = Math.max(0, (Date.now() - then) / 1000);
	if (secs < 60) return "just now";
	const mins = Math.floor(secs / 60);
	if (mins < 60) return `${mins}m ago`;
	const hours = Math.floor(mins / 60);
	if (hours < 24) return `${hours}h ago`;
	const days = Math.floor(hours / 24);
	if (days < 7) return `${days}d ago`;
	return new Date(then).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Whole seconds → "8s" / "1m 12s". Used for "how long did that take". */
export function formatDuration(seconds) {
	const s = Math.max(0, Math.round(seconds));
	return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
}

/** How long a row took to settle: created → last written. Null when it is too
 * short to be interesting (a sub-2s reply doesn't need a stopwatch on it). */
export function spanBetween(from, to, minSeconds = 2) {
	const t0 = parseTs(from);
	const t1 = parseTs(to);
	if (Number.isNaN(t0) || Number.isNaN(t1) || t1 <= t0) return "";
	const s = Math.round((t1 - t0) / 1000);
	if (s < minSeconds) return "";
	return formatDuration(s);
}
