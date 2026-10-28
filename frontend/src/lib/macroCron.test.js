// frontend/src/lib/macroCron.test.js
import { test } from "node:test";
import assert from "node:assert/strict";
import {
	cronLinesOf,
	cronListLabel,
	parseCron,
	presetCron,
	scheduleFromRow,
	wallClock,
	schedulePayload,
	schedulePreview,
	scheduleSentence,
	shortestGapMinutes,
} from "./macroCron.js";

// Thursday 22 Oct 2026, 10:00 on the site's clock.
const NOW = wallClock(2026, 9, 22, 10, 0);
const base = {
	frequency: "weekly",
	times: [{ h: "9", m: "30", ap: "AM" }],
	days: [1, 3, 5],
	dom: [1],
	everyN: "1",
	hourlyMin: "00",
	cron: "",
};

test("weekly days and a time are one cron line", () => {
	assert.deepEqual(presetCron(base), ["30 9 * * 1,3,5"]);
});

test("times that share a minute share a line; other minutes get their own", () => {
	const s = {
		...base,
		frequency: "daily",
		times: [
			{ h: "6", m: "00", ap: "PM" },
			{ h: "9", m: "00", ap: "AM" },
			{ h: "1", m: "30", ap: "PM" },
		],
	};
	assert.deepEqual(presetCron(s), ["0 9,18 * * *", "30 13 * * *"]);
});

test("12 AM is midnight and 12 PM is noon", () => {
	const s = {
		...base,
		frequency: "daily",
		times: [
			{ h: "12", m: "00", ap: "AM" },
			{ h: "12", m: "00", ap: "PM" },
		],
	};
	assert.deepEqual(presetCron(s), ["0 0,12 * * *"]);
});

test("hourly is a step on the hour", () => {
	assert.deepEqual(presetCron({ ...base, frequency: "hourly", everyN: "2", hourlyMin: "15" }), [
		"15 */2 * * *",
	]);
	assert.deepEqual(presetCron({ ...base, frequency: "hourly", everyN: "1", hourlyMin: "00" }), [
		"0 * * * *",
	]);
});

test("monthly days are the day-of-month part", () => {
	assert.deepEqual(presetCron({ ...base, frequency: "monthly", dom: [15, 1] }), [
		"30 9 1,15 * *",
	]);
});

test("a weekly schedule with no day cannot be saved, and says why", () => {
	const p = schedulePreview({ ...base, days: [] }, { now: NOW });
	assert.equal(p.error, "Pick at least one day.");
});

test("every preset reads back as itself from what it saves", () => {
	const cases = [
		{ ...base },
		{
			...base,
			frequency: "daily",
			times: [
				{ h: "9", m: "00", ap: "AM" },
				{ h: "6", m: "30", ap: "PM" },
			],
		},
		{ ...base, frequency: "monthly", dom: [1, 15, 31] },
		{ ...base, frequency: "hourly", everyN: "6", hourlyMin: "45" },
	];
	for (const s of cases) {
		const row = schedulePayload(s);
		const back = scheduleFromRow(row);
		assert.equal(back.frequency, s.frequency);
		assert.deepEqual(presetCron(back), presetCron(s));
	}
});

test("a macro from before cron opens on its old time, weekday and day", () => {
	const s = scheduleFromRow({
		schedule_frequency: "weekly",
		schedule_time: "14:45:00",
		schedule_weekday: "Friday",
	});
	assert.deepEqual(s.times, [{ h: "2", m: "45", ap: "PM" }]);
	assert.deepEqual(s.days, [5]);
	assert.deepEqual(presetCron(s), ["45 14 * * 5"]);
	const m = scheduleFromRow({ schedule_frequency: "monthly", schedule_day_of_month: 20 });
	assert.deepEqual(presetCron(m), ["0 9 20 * *"]);
});

test("a cron the form did not write opens as custom with its text", () => {
	const s = scheduleFromRow({
		schedule_frequency: "weekly",
		schedule_cron: "*/30 9-17 * * 1-5",
	});
	assert.equal(s.frequency, "custom");
	assert.equal(s.cron, "*/30 9-17 * * 1-5");
});

test("the payload keeps the first time, weekday and day for older readers", () => {
	const p = schedulePayload({ ...base, days: [5, 1], times: [{ h: "1", m: "05", ap: "PM" }] });
	assert.equal(p.schedule_time, "13:05");
	assert.equal(p.schedule_weekday, "Monday");
	assert.equal(p.schedule_day_of_month, 0);
	assert.equal(p.schedule_date, "");
	assert.equal(p.schedule_cron, "5 13 * * 1,5");
});

test("a one-time payload has no cron and keeps its date", () => {
	const p = schedulePayload({ ...base, frequency: "once" }, "2026-10-30");
	assert.equal(p.schedule_cron, "");
	assert.equal(p.schedule_date, "2026-10-30");
});

test("the next runs come in order from now", () => {
	const p = schedulePreview(base, { now: NOW });
	assert.equal(p.error, "");
	assert.deepEqual(p.next, [
		"Fri, 23 Oct at 9:30 am",
		"Mon, 26 Oct at 9:30 am",
		"Wed, 28 Oct at 9:30 am",
	]);
	assert.equal(p.summary, "Runs every Mon, Wed and Fri at 9:30 am.");
});

test("a run already past today is not offered", () => {
	const p = schedulePreview({ ...base, days: [4] }, { now: NOW });
	assert.equal(p.next[0], "Thu, 29 Oct at 9:30 am");
});

test("the month estimate is measured against the step budget", () => {
	const s = { ...base, frequency: "hourly", everyN: "1", hourlyMin: "00" };
	const p = schedulePreview(s, { now: NOW, steps: 4, budget: 500 });
	// Every hour strictly between now and 30 days on: 30 x 24 less the one at 10:00 now.
	assert.equal(p.perMonth, 719);
	assert.equal(p.stepsMonth, 2876);
	assert.equal(p.overBudget, true);
	assert.equal(schedulePreview(base, { now: NOW, steps: 1, budget: 500 }).overBudget, false);
});

// The same table as jarvis/tests/test_macro_cron_schedule.py: one rule, two files.
const GAPS = [
	["* * * * *", 1],
	["* 9 * * *", 1],
	["*/3 9 * * *", 3],
	["*/7 * * * *", 4],
	["0,2 9 * * *", 2],
	["0 9 * * *\n2 9 * * *", 2],
	["58 23 * * *\n0 0 * * *", 2],
	["*/5 * * * *", 5],
	["0 9 * * MON-FRI", 1440],
	["0 * * * *", 60],
	["15 */2 * * *", 120],
	["30 9 * * *", 1440],
];

test("the shortest gap between runs, over all lines and across midnight", () => {
	for (const [cron, gap] of GAPS) {
		assert.equal(shortestGapMinutes(cronLinesOf(cron).map(parseCron)), gap, cron);
	}
});

test("runs under five minutes apart are an error that says how far apart", () => {
	for (const [cron, gap] of GAPS.filter(([, g]) => g < 5)) {
		const p = schedulePreview({ ...base, frequency: "custom", cron }, { now: NOW });
		assert.match(p.error, new RegExp(`${gap} minute\\(s\\) apart`), cron);
		assert.match(p.error, /at least 5 minutes/, cron);
	}
	for (const [cron] of GAPS.filter(([, g]) => g >= 5)) {
		const p = schedulePreview({ ...base, frequency: "custom", cron }, { now: NOW });
		assert.equal(p.error, "", cron);
	}
});

test("only a star in the minute part gets the every-minute hint", () => {
	const star = schedulePreview(
		{ ...base, frequency: "custom", cron: "* 9 * * *" },
		{ now: NOW }
	);
	assert.match(star.error, /write “0 9 \* \* \*”/);
	const list = schedulePreview(
		{ ...base, frequency: "custom", cron: "0,2 9 * * *" },
		{ now: NOW }
	);
	assert.doesNotMatch(list.error, /every minute/);
});

test("a minute that is not a multiple of five is only a late-start note", () => {
	const s = { ...base, frequency: "custom", cron: "7 9 * * *" };
	const p = schedulePreview(s, { now: NOW });
	assert.equal(p.tooClose, true);
	assert.equal(p.error, "");
	assert.equal(schedulePreview(base, { now: NOW }).tooClose, false);
});

test("what the server refuses the form refuses too, and names work", () => {
	for (const cron of [
		"0 9 L * *",
		"5-1 * * * *",
		"0 9 * * fri-mon",
		"0 9 * * 1#2",
		"0 9 ? * *",
		// A name belongs to its own field only.
		"jan 9 * * *",
		"0 mon * * *",
		"0 9 mon * *",
		"0 9 * * jan",
		// croniter reads "n-n" as "*", so the server refuses it too.
		"3-3 9 * * *",
		"0 9-9 * * *",
		"0 9 * * 2-2",
		"0 9 * jan-jan *",
	]) {
		const p = schedulePreview({ ...base, frequency: "custom", cron }, { now: NOW });
		assert.notEqual(p.error, "", cron);
	}
	for (const cron of ["0 9 * jan-mar *", "0 9 * * sun-sat", "0 9 * * MON-FRI"]) {
		const p = schedulePreview({ ...base, frequency: "custom", cron }, { now: NOW });
		assert.equal(p.error, "", cron);
		assert.ok(p.next.length > 0, cron);
	}
});

test("a monthly day past the 28th says which months skip it", () => {
	const monthly = { ...base, frequency: "monthly" };
	const short = (dom) => schedulePreview({ ...monthly, dom }, { now: NOW }).shortMonths;
	assert.equal(short([31]), "31st");
	assert.equal(short([15, 30]), "30th");
	assert.equal(short([29]), "29th");
	assert.equal(short([28]), "");
	assert.equal(schedulePreview(base, { now: NOW }).shortMonths, "");
});

test("custom cron errors name the part that is wrong", () => {
	const p = schedulePreview({ ...base, frequency: "custom", cron: "61 9 * * *" }, { now: NOW });
	assert.match(p.error, /minute part “61” must be between 0 and 59/);
	const q = schedulePreview({ ...base, frequency: "custom", cron: "0 9 * *" }, { now: NOW });
	assert.match(q.error, /5 parts/);
	const r = schedulePreview({ ...base, frequency: "custom", cron: "0 0 30 2 *" }, { now: NOW });
	assert.match(r.error, /never comes round/);
});

test("cron reads day of month OR weekday when both are set, as the server does", () => {
	const c = parseCron("0 9 1 * 1");
	const p = schedulePreview({ ...base, frequency: "custom", cron: "0 9 1 * 1" }, { now: NOW });
	assert.equal(c.dom.has(1) && c.dow.has(1), true);
	assert.deepEqual(p.next, [
		"Mon, 26 Oct at 9:00 am",
		"Sun, 1 Nov at 9:00 am",
		"Mon, 2 Nov at 9:00 am",
	]);
});

test("a one-time schedule in the past says so", () => {
	const p = schedulePreview(
		{ ...base, frequency: "once" },
		{ now: NOW, scheduleDate: "2026-10-21" }
	);
	assert.match(p.error, /passed/);
	const q = schedulePreview(
		{ ...base, frequency: "once" },
		{ now: NOW, scheduleDate: "2026-10-23" }
	);
	assert.equal(q.next[0], "Fri, 23 Oct 2026 at 9:30 am");
});

test("sentences for each preset", () => {
	assert.equal(
		scheduleSentence({ ...base, days: [1, 2, 3, 4, 5] }),
		"Runs every weekday at 9:30 am."
	);
	assert.equal(
		scheduleSentence({ ...base, frequency: "monthly", dom: [15, 1] }),
		"Runs on the 1st and 15th of every month at 9:30 am."
	);
	assert.equal(
		scheduleSentence({ ...base, frequency: "hourly", everyN: "2", hourlyMin: "15" }),
		"Runs every 2 hours at 15 minutes past."
	);
});

test("list labels", () => {
	assert.equal(
		cronListLabel({ schedule_frequency: "weekly", schedule_cron: "30 9 * * 1,3,5" }),
		"Weekly on Mon, Wed and Fri · 9:30 am"
	);
	assert.equal(
		cronListLabel({ schedule_frequency: "weekly", schedule_cron: "0 9 * * 1,2,3,4,5" }),
		"Weekdays · 9:00 am"
	);
	assert.equal(
		cronListLabel({ schedule_frequency: "hourly", schedule_cron: "15 */2 * * *" }),
		"Every 2 hours at :15"
	);
	assert.equal(
		cronListLabel({ schedule_frequency: "custom", schedule_cron: "*/30 9-17 * * 1-5" }),
		"Custom · */30 9-17 * * 1-5"
	);
	assert.equal(cronListLabel({ schedule_frequency: "daily", schedule_cron: "" }), "");
	assert.equal(cronListLabel({ schedule_frequency: "once", schedule_cron: "0 9 * * *" }), "");
});
