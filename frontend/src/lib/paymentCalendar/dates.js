// Calendar arithmetic only. Financial amounts and projections are server-owned.
function date(value) {
	if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
	const parsed = new Date(value + "T12:00:00Z");
	return !isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value ? parsed : null;
}

export function addDays(value, count) {
	const parsed = date(value);
	if (!parsed || !Number.isInteger(count)) return "";
	parsed.setUTCDate(parsed.getUTCDate() + count);
	return parsed.toISOString().slice(0, 10);
}

export function addMonths(month, count) {
	const parsed = date(month + "-01");
	if (!parsed || !Number.isInteger(count)) return "";
	parsed.setUTCMonth(parsed.getUTCMonth() + count);
	return parsed.toISOString().slice(0, 7);
}

export function monthGrid(month) {
	const first = date(month + "-01");
	if (!first) return [];
	const start = addDays(month + "-01", -first.getUTCDay());
	return Array.from({ length: 42 }, (_, index) => addDays(start, index));
}

export function isSelectable(value, scope) {
	return (
		!!date(value) &&
		!!scope &&
		value >= scope.selectable_start &&
		value <= scope.selectable_end
	);
}

export function dateLabel(value, full = false) {
	const parsed = date(value);
	return parsed
		? new Intl.DateTimeFormat("en-IN", {
				timeZone: "UTC",
				day: "numeric",
				month: full ? "long" : "short",
				year: "numeric",
				...(full ? { weekday: "short" } : {}),
		  }).format(parsed)
		: "Unavailable date";
}

export function monthLabel(month) {
	const parsed = date(month + "-01");
	return parsed
		? new Intl.DateTimeFormat("en-IN", {
				timeZone: "UTC",
				month: "long",
				year: "numeric",
		  }).format(parsed)
		: "";
}

export function siteToday(zone, epoch = Date.now()) {
	if (!zone) return "";
	try {
		const parts = new Intl.DateTimeFormat("en-CA", {
			timeZone: zone,
			year: "numeric",
			month: "2-digit",
			day: "2-digit",
		}).formatToParts(new Date(epoch));
		const part = (type) => parts.find((value) => value.type === type)?.value;
		return `${part("year")}-${part("month")}-${part("day")}`;
	} catch {
		return "";
	}
}

export function msUntilSiteMidnight(zone, epoch = Date.now()) {
	const today = siteToday(zone, epoch);
	if (!today) return 60000;
	// Find the date boundary itself, including 23/25-hour days, without assuming
	// a fixed UTC offset. Current supported IANA site zones cross within 36 hours.
	let low = epoch,
		high = epoch + 36 * 3600000;
	while (high - low > 1) {
		const middle = Math.floor((high + low) / 2);
		if (siteToday(zone, middle) === today) low = middle;
		else high = middle;
	}
	return high - epoch;
}

export function snapshotTime(value, zone) {
	try {
		return new Intl.DateTimeFormat("en-IN", {
			timeZone: zone || "UTC",
			dateStyle: "medium",
			timeStyle: "short",
		}).format(new Date(value));
	} catch {
		return "Unavailable time";
	}
}
