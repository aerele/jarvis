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
	const days = new Date(
		Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0)
	).getUTCDate();
	const weeks = Math.ceil((first.getUTCDay() + days) / 7);
	return Array.from({ length: weeks * 7 }, (_, index) => addDays(start, index));
}

// Monday-start week containing the date.
export function weekStrip(value) {
	const parsed = date(value);
	if (!parsed) return [];
	const monday = addDays(value, -((parsed.getUTCDay() + 6) % 7));
	return Array.from({ length: 7 }, (_, index) => addDays(monday, index));
}

// "26 Oct – 1 Nov": the Monday–Sunday week containing the date.
export function weekLabel(value) {
	const strip = weekStrip(value);
	if (!strip.length) return "";
	return `${dayMonthLabel(strip[0])} \u2013 ${dayMonthLabel(strip[6])}`;
}

// Fixed table: en-IN data in some runtimes writes September as "Sept".
const SHORT_MONTHS = [
	"Jan",
	"Feb",
	"Mar",
	"Apr",
	"May",
	"Jun",
	"Jul",
	"Aug",
	"Sep",
	"Oct",
	"Nov",
	"Dec",
];

function utcFormat(value, options) {
	const parsed = date(value);
	if (!parsed) return "";
	const parts = new Intl.DateTimeFormat("en-IN", {
		timeZone: "UTC",
		...options,
	}).formatToParts(parsed);
	return parts
		.map((part) =>
			part.type === "month" && options.month === "short"
				? SHORT_MONTHS[parsed.getUTCMonth()]
				: part.value
		)
		.join("");
}

// "11 Nov"
export const dayMonthLabel = (value) => utcFormat(value, { day: "numeric", month: "short" });

// "Wednesday, 11 Nov"
export const fullDateLabel = (value) =>
	utcFormat(value, { weekday: "long", day: "numeric", month: "short" });

// "Mon"
export const weekdayShort = (value) => utcFormat(value, { weekday: "short" });

export function isSelectable(value, scope) {
	return (
		!!date(value) &&
		!!scope &&
		value >= scope.selectable_start &&
		value <= scope.selectable_end
	);
}

export function dateLabel(value, full = false) {
	return (
		utcFormat(value, {
			day: "numeric",
			month: full ? "long" : "short",
			year: "numeric",
			...(full ? { weekday: "short" } : {}),
		}) || "Unavailable date"
	);
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

// "11 Nov, 10:42 IST": the template's snapshot time, in the site zone.
export function snapshotStamp(value, zone) {
	try {
		const parts = new Intl.DateTimeFormat("en-IN", {
			timeZone: zone || "UTC",
			day: "numeric",
			month: "numeric",
			hour: "2-digit",
			minute: "2-digit",
			hourCycle: "h23",
			timeZoneName: "short",
		}).formatToParts(new Date(value));
		const part = (type) => parts.find((item) => item.type === type).value;
		return `${part("day")} ${SHORT_MONTHS[Number(part("month")) - 1]}, ${part("hour")}:${part(
			"minute"
		)} ${part("timeZoneName")}`;
	} catch {
		return "Unavailable time";
	}
}
