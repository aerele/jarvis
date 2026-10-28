// frontend/src/lib/macroCron.js
// A macro's schedule as the form edits it, and the cron lines it is saved as.
// Dependency-free so `node --test` can hold it.
//
// The form's state: { frequency, times: [{ h: "9", m: "30", ap: "AM" }], days: [cron
// weekday numbers, 0 = Sunday], dom: [days of the month], everyN: "2", hourlyMin: "15",
// cron: "0 9 * * 1-5" }. Daily, weekly, monthly and hourly are saved as cron lines the
// server runs on (macro_scheduler.macro_next_run), one line per distinct minute of
// the times; custom is the owner's own cron; once keeps its date and first time.
//
// Every Date here is a wall-clock time on the SITE's clock (the one the scheduler
// fires on), held in UTC parts (`wallClock`) so the viewer's own zone and its
// daylight-saving jumps never shift it: the caller passes "now" built the same way.

export const MAX_TIMES = 6;
export const MAX_CRON_LINES = 6;
// The sweep looks for due macros every 5 minutes (the server's MIN_GAP_MINUTES), so two
// runs closer than that would merge into one.
export const MIN_GAP_MINUTES = 5;
export const FREQUENCIES = ["once", "hourly", "daily", "weekly", "monthly", "custom"];
export const HOURS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"];
// Five-minute steps: the scheduler looks every five minutes, so a finer minute could
// not be kept anyway.
export const MINUTES = Array.from({ length: 12 }, (_, i) => String(i * 5).padStart(2, "0"));
export const EVERY_N_HOURS = ["1", "2", "3", "4", "6", "8", "12"];
// Monday first, as the week reads on the form; the values are cron's (0 = Sunday).
export const WEEK = [1, 2, 3, 4, 5, 6, 0];
export const DAY_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
export const DAY_LONG = [
	"Sunday",
	"Monday",
	"Tuesday",
	"Wednesday",
	"Thursday",
	"Friday",
	"Saturday",
];
const MONTH_SHORT = [
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
const NAMES = {
	sun: 0,
	mon: 1,
	tue: 2,
	wed: 3,
	thu: 4,
	fri: 5,
	sat: 6,
	jan: 1,
	feb: 2,
	mar: 3,
	apr: 4,
	may: 5,
	jun: 6,
	jul: 7,
	aug: 8,
	sep: 9,
	oct: 10,
	nov: 11,
	dec: 12,
};
const DAY_MS = 24 * 3600 * 1000;

export function wallClock(year, month, day, hour = 0, minute = 0) {
	return new Date(Date.UTC(year, month, day, hour, minute));
}

const pad = (n) => String(n).padStart(2, "0");
const byNumber = (a, b) => a - b;

// ── times ────────────────────────────────────────────────────────────────────
export function to24(t) {
	const h = Number(t.h) % 12;
	return t.ap === "PM" ? h + 12 : h;
}

export function from24(hour, minute) {
	return { h: String(hour % 12 || 12), m: pad(minute), ap: hour < 12 ? "AM" : "PM" };
}

// "09:30" from a stored time ("09:30:00", "9:30"), or null.
function hhmmParts(value) {
	const m = /^(\d{1,2}):(\d{2})/.exec(String(value || ""));
	return m ? [Number(m[1]) % 24, Number(m[2]) % 60] : null;
}

export function timeText(hour, minute) {
	return `${hour % 12 || 12}:${pad(minute)} ${hour < 12 ? "am" : "pm"}`;
}

function sortedTimes(times) {
	return (times || [])
		.map((t) => ({ hour: to24(t), minute: Number(t.m) }))
		.sort((a, b) => a.hour * 60 + a.minute - (b.hour * 60 + b.minute));
}

export function words(list) {
	if (list.length < 2) return list.join("");
	return `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`;
}

export function ordinal(n) {
	const v = n % 100;
	if (v >= 11 && v <= 13) return `${n}th`;
	return `${n}${["th", "st", "nd", "rd"][n % 10] || "th"}`;
}

// ── cron ─────────────────────────────────────────────────────────────────────
function parseField(src, min, max, label) {
	const out = new Set();
	for (const part of src.split(",")) {
		const m = /^(\*|[a-z0-9]+(?:-[a-z0-9]+)?)(?:\/(\d+))?$/i.exec(part);
		const bad = new Error(`The ${label} part “${part}” is not something cron understands.`);
		if (!m) throw bad;
		let lo = min;
		let hi = max;
		if (m[1] !== "*") {
			const ends = m[1].split("-").map((x) => {
				const k = x.toLowerCase();
				if (k in NAMES) return NAMES[k];
				if (!/^\d+$/.test(x)) throw bad;
				return Number(x);
			});
			lo = ends[0];
			hi = ends.length > 1 ? ends[1] : m[2] ? max : ends[0];
		}
		const step = m[2] ? Number(m[2]) : 1;
		if (step < 1 || lo < min || hi > max || lo > hi) {
			throw new Error(`The ${label} part “${part}” must be between ${min} and ${max}.`);
		}
		for (let v = lo; v <= hi; v += step) out.add(v);
	}
	return out;
}

// One cron line, read the way the server's croniter reads it (day of month and
// weekday both restricted means either one). Throws with a sentence for the owner.
export function parseCron(expr) {
	const f = String(expr || "")
		.trim()
		.split(/\s+/);
	if (f.length !== 5 || !f[0]) {
		throw new Error("A cron schedule has 5 parts: minute, hour, day, month and weekday.");
	}
	const dow = parseField(f[4], 0, 7, "weekday");
	if (dow.has(7)) {
		dow.delete(7);
		dow.add(0);
	}
	return {
		minutes: [...parseField(f[0], 0, 59, "minute")].sort(byNumber),
		hours: [...parseField(f[1], 0, 23, "hour")].sort(byNumber),
		dom: parseField(f[2], 1, 31, "day"),
		months: parseField(f[3], 1, 12, "month"),
		dow,
		domAny: f[2] === "*",
		dowAny: f[4] === "*",
	};
}

function dayMatches(c, d) {
	if (!c.months.has(d.getUTCMonth() + 1)) return false;
	const onDate = c.dom.has(d.getUTCDate());
	const onWeekday = c.dow.has(d.getUTCDay());
	if (c.domAny && c.dowAny) return true;
	if (c.domAny) return onWeekday;
	if (c.dowAny) return onDate;
	return onDate || onWeekday;
}

// The slots of the parsed lines strictly after `from` and before `to`, at most about
// `limit` of them, in order and without repeats.
export function cronRuns(parsed, from, to, limit = 3) {
	const out = [];
	const day = wallClock(from.getUTCFullYear(), from.getUTCMonth(), from.getUTCDate());
	while (day < to && out.length < limit) {
		for (const c of parsed) {
			if (!dayMatches(c, day)) continue;
			for (const h of c.hours) {
				for (const m of c.minutes) {
					const t = wallClock(
						day.getUTCFullYear(),
						day.getUTCMonth(),
						day.getUTCDate(),
						h,
						m
					);
					if (t > from && t < to) out.push(t);
				}
			}
		}
		day.setUTCDate(day.getUTCDate() + 1);
	}
	out.sort(byNumber);
	return out.filter((t, i) => i === 0 || +t !== +out[i - 1]);
}

// The fewest minutes between two runs of the parsed lines, as the server counts them
// (macro_scheduler.shortest_gap_minutes): days are ignored, so it errs on the side of
// refusing, and the last run of a day is followed by the first of the next.
export function shortestGapMinutes(parsed) {
	const slots = new Set();
	for (const c of parsed) {
		for (const h of c.hours) for (const m of c.minutes) slots.add(h * 60 + m);
	}
	const ordered = [...slots].sort(byNumber);
	const gaps = ordered.slice(1).map((slot, i) => slot - ordered[i]);
	return Math.min(...gaps, ordered[0] + 1440 - ordered[ordered.length - 1]);
}

// The sentence for a gap under MIN_GAP_MINUTES: the server says the same words.
function gapProblem(gap, lines) {
	let text =
		`Two runs of this schedule can be ${gap} minute(s) apart. Macros are checked every ` +
		`${MIN_GAP_MINUTES} minutes, so leave at least ${MIN_GAP_MINUTES} minutes between runs.`;
	if (lines.some((line) => line.split(" ")[0] === "*")) {
		text +=
			" A “*” in the first part means every minute: for 9:00 each day write “0 9 * * *”.";
	}
	return text;
}

export function cronLinesOf(text) {
	return String(text || "")
		.split("\n")
		.map((l) => l.trim().split(/\s+/).join(" "))
		.filter(Boolean);
}

// ── the form's state <-> what is saved ───────────────────────────────────────
// The cron lines a preset is saved as. Throws with a sentence when the state cannot
// be saved (no day picked).
export function presetCron(state) {
	const s = state;
	if (s.frequency === "custom") return cronLinesOf(s.cron);
	if (s.frequency === "hourly") {
		const n = Number(s.everyN) || 1;
		return [`${Number(s.hourlyMin) || 0} ${n === 1 ? "*" : `*/${n}`} * * *`];
	}
	let dayPart = "* * *";
	if (s.frequency === "weekly") {
		if (!s.days.length) throw new Error("Pick at least one day.");
		dayPart = `* * ${[...new Set(s.days)].sort(byNumber).join(",")}`;
	} else if (s.frequency === "monthly") {
		if (!s.dom.length) throw new Error("Pick at least one day of the month.");
		dayPart = `${[...new Set(s.dom)].sort(byNumber).join(",")} * *`;
	}
	const hoursByMinute = new Map();
	for (const t of sortedTimes(s.times)) {
		if (!hoursByMinute.has(t.minute)) hoursByMinute.set(t.minute, new Set());
		hoursByMinute.get(t.minute).add(t.hour);
	}
	return [...hoursByMinute.keys()]
		.sort(byNumber)
		.map((m) => `${m} ${[...hoursByMinute.get(m)].sort(byNumber).join(",")} ${dayPart}`);
}

const LIST = String.raw`\d+(?:,\d+)*`;
const PRESET_LINE = new RegExp(String.raw`^(\d+) (${LIST}) (\*|${LIST}) \* (\*|${LIST})$`);

// The preset a saved cron reads as, or null when it is not one the form writes.
function presetFromCron(frequency, lines) {
	if (frequency === "hourly") {
		const m = lines.length === 1 && /^(\d+) (\*|\*\/(\d+)) \* \* \*$/.exec(lines[0]);
		if (!m || Number(m[1]) > 59) return null;
		const n = m[3] || "1";
		if (!EVERY_N_HOURS.includes(n)) return null;
		return { everyN: n, hourlyMin: pad(Number(m[1])) };
	}
	if (!["daily", "weekly", "monthly"].includes(frequency)) return null;
	const times = [];
	let dayKey = null;
	for (const line of lines) {
		const m = PRESET_LINE.exec(line);
		if (!m) return null;
		const [, minute, hours, domPart, dowPart] = m;
		const key = `${domPart} ${dowPart}`;
		if (dayKey !== null && key !== dayKey) return null;
		dayKey = key;
		if (frequency === "daily" && key !== "* *") return null;
		if (frequency === "weekly" && (domPart !== "*" || dowPart === "*")) return null;
		if (frequency === "monthly" && (domPart === "*" || dowPart !== "*")) return null;
		for (const h of hours.split(",")) {
			if (Number(h) > 23 || Number(minute) > 59) return null;
			times.push(from24(Number(h), Number(minute)));
		}
	}
	if (!times.length || times.length > MAX_TIMES) return null;
	const [domPart, dowPart] = dayKey.split(" ");
	const out = {
		times: times.sort((a, b) => to24(a) * 60 + +a.m - (to24(b) * 60 + +b.m)),
	};
	if (frequency === "weekly") {
		const days = dowPart.split(",").map((d) => Number(d) % 7);
		if (days.some((d) => d > 6)) return null;
		out.days = [...new Set(days)];
	}
	if (frequency === "monthly") {
		const dom = domPart.split(",").map(Number);
		if (dom.some((d) => d < 1 || d > 31)) return null;
		out.dom = [...new Set(dom)];
	}
	return out;
}

const WEEKDAY_NUMBER = Object.fromEntries(DAY_LONG.map((d, i) => [d, i]));

// The form's state for a saved macro (get_macro / a list row). A macro from before
// cron schedules is read from its time, weekday and day of month; a cron the form
// did not write opens as custom, so nothing is lost.
export function scheduleFromRow(row = {}) {
	const time = hhmmParts(row.schedule_time) || [9, 0];
	let frequency = FREQUENCIES.includes(row.schedule_frequency)
		? row.schedule_frequency
		: "daily";
	const weekday = WEEKDAY_NUMBER[row.schedule_weekday];
	const state = {
		frequency,
		times: [from24(time[0], time[1])],
		days: [weekday === undefined ? 1 : weekday],
		dom: [Number(row.schedule_day_of_month) || 1],
		everyN: "1",
		hourlyMin: "00",
		cron: "",
	};
	const lines = cronLinesOf(row.schedule_cron);
	if (frequency === "once" || !lines.length) {
		if (frequency === "custom") state.cron = "0 9 * * 1-5";
		return state;
	}
	const preset = presetFromCron(frequency, lines);
	if (preset) return { ...state, ...preset };
	return { ...state, frequency: "custom", cron: lines.join("\n") };
}

// What a save sends for the schedule. The old fields still carry the first time,
// weekday and day, so a server or screen that does not know cron yet reads a near
// enough schedule.
export function schedulePayload(state, scheduleDate = "") {
	const s = state;
	const first = sortedTimes(s.times)[0] || { hour: 9, minute: 0 };
	let cron = "";
	if (s.frequency !== "once") {
		try {
			cron = presetCron(s).join("\n");
		} catch (e) {
			cron = "";
		}
	}
	const firstDay = WEEK.find((d) => s.days.includes(d));
	return {
		schedule_frequency: s.frequency,
		schedule_cron: cron,
		schedule_time: `${pad(first.hour)}:${pad(first.minute)}`,
		schedule_weekday:
			s.frequency === "weekly" && firstDay !== undefined ? DAY_LONG[firstDay] : "",
		schedule_day_of_month: s.frequency === "monthly" && s.dom.length ? Math.min(...s.dom) : 0,
		schedule_date: s.frequency === "once" ? scheduleDate || "" : "",
	};
}

// ── what the form and the lists say ──────────────────────────────────────────
function daysPhrase(days) {
	const key = [...new Set(days)].sort(byNumber).join(",");
	if (key === "1,2,3,4,5") return "every weekday";
	if (key === "0,6") return "every Saturday and Sunday";
	if (key === "0,1,2,3,4,5,6") return "every day";
	return `every ${words(WEEK.filter((d) => days.includes(d)).map((d) => DAY_SHORT[d]))}`;
}

function domPhrase(dom) {
	return `on the ${words([...new Set(dom)].sort(byNumber).map(ordinal))} of every month`;
}

// "Runs every Mon, Wed and Fri at 9:30 am." for a preset; custom has its own line.
export function scheduleSentence(state) {
	const s = state;
	const at = words(sortedTimes(s.times).map((t) => timeText(t.hour, t.minute)));
	if (s.frequency === "hourly") {
		const n = Number(s.everyN) || 1;
		return `Runs ${n === 1 ? "every hour" : `every ${n} hours`} at ${
			s.hourlyMin
		} minutes past.`;
	}
	if (s.frequency === "daily") return `Runs every day at ${at}.`;
	if (s.frequency === "weekly") return `Runs ${daysPhrase(s.days)} at ${at}.`;
	if (s.frequency === "monthly") return `Runs ${domPhrase(s.dom)} at ${at}.`;
	return "Runs on its cron schedule.";
}

export function runText(d, withYear = false) {
	return (
		`${DAY_SHORT[d.getUTCDay()]}, ${d.getUTCDate()} ${MONTH_SHORT[d.getUTCMonth()]}` +
		`${withYear ? ` ${d.getUTCFullYear()}` : ""} at ` +
		timeText(d.getUTCHours(), d.getUTCMinutes())
	);
}

// Everything the schedule's summary box says, from the draft state. `now` is the
// site's wall clock; `steps` the macro's step count; `budget` the monthly step budget
// for scheduled runs (0 when unknown).
export function schedulePreview(state, { now, scheduleDate = "", steps = 1, budget = 0 }) {
	const out = {
		error: "",
		summary: "",
		next: [],
		perMonth: 0,
		stepsMonth: 0,
		overBudget: false,
		tooClose: false,
		shortMonths: "",
		cronText: "",
	};
	try {
		if (state.frequency === "once") {
			const p = String(scheduleDate || "")
				.split("-")
				.map(Number);
			if (p.length !== 3 || !p[0]) throw new Error("Pick the date it runs on.");
			const first = sortedTimes(state.times)[0] || { hour: 9, minute: 0 };
			const at = wallClock(p[0], p[1] - 1, p[2], first.hour, first.minute);
			if (at <= now) throw new Error("That time has passed. Pick a later one.");
			out.summary = `Runs once on ${runText(
				at,
				true
			)}, then the schedule switches itself off.`;
			out.next = [runText(at, true)];
			return out;
		}
		const lines = presetCron(state);
		if (!lines.length) throw new Error("Write a cron schedule.");
		if (lines.length > MAX_CRON_LINES) {
			throw new Error(`A schedule can have at most ${MAX_CRON_LINES} cron lines.`);
		}
		const parsed = lines.map(parseCron);
		const found = cronRuns(parsed, now, new Date(+now + 400 * DAY_MS), 4);
		if (!found.length)
			throw new Error("This schedule never comes round. Check its day and month.");
		const gap = shortestGapMinutes(parsed);
		if (gap < MIN_GAP_MINUTES) throw new Error(gapProblem(gap, lines));
		out.next = found.slice(0, 3).map((d) => runText(d));
		const month = cronRuns(parsed, now, new Date(+now + 30 * DAY_MS), 50000);
		out.perMonth = month.length;
		out.stepsMonth = month.length * Math.max(1, Number(steps) || 1);
		out.overBudget = budget > 0 && out.stepsMonth > budget;
		out.tooClose = parsed.some((c) => c.minutes.some((m) => m % 5));
		if (state.frequency === "monthly" && Math.max(...state.dom) > 28) {
			out.shortMonths = ordinal(Math.max(...state.dom));
		}
		out.cronText = lines.join("  ·  ");
		out.summary =
			state.frequency === "custom"
				? `Runs on a custom schedule, ${out.perMonth} time${
						out.perMonth === 1 ? "" : "s"
				  } in the next 30 days.`
				: scheduleSentence(state);
	} catch (e) {
		out.error = e.message;
	}
	return out;
}

// What a cron schedule reads as in a list ("Weekly on Mon, Wed · 9:30 am"), or ""
// when the row has none (the caller keeps the old label).
export function cronListLabel(row) {
	if (row.schedule_frequency === "once" || !cronLinesOf(row.schedule_cron).length) return "";
	const s = scheduleFromRow(row);
	const at = sortedTimes(s.times)
		.map((t) => timeText(t.hour, t.minute))
		.join(", ");
	if (s.frequency === "hourly") {
		const n = Number(s.everyN) || 1;
		return `${n === 1 ? "Hourly" : `Every ${n} hours`} at :${s.hourlyMin}`;
	}
	if (s.frequency === "daily") return `Daily · ${at}`;
	if (s.frequency === "weekly") {
		const phrase = daysPhrase(s.days);
		if (phrase === "every weekday") return `Weekdays · ${at}`;
		if (phrase === "every day") return `Daily · ${at}`;
		return `Weekly on ${phrase.replace(/^every /, "")} · ${at}`;
	}
	if (s.frequency === "monthly") {
		return `Monthly on the ${[...new Set(s.dom)]
			.sort(byNumber)
			.map(ordinal)
			.join(", ")} · ${at}`;
	}
	return `Custom · ${cronLinesOf(s.cron).join(" · ")}`;
}
