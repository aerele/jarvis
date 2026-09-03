// Shared weekly/monthly schedule-anchor helpers (jarvis#653) — the "which day"
// half of the Schedule section's Frequency/Time/Day trio, used by AgentDetail.vue,
// MacroDetail.vue and MacrosList.vue so the three surfaces cannot drift on the
// option lists, the ordinal spelling, or the summary wording.
//
// The Day control's model value is always a STRING (a native <select>'s
// modelValue, matching FormControl's own Frequency control): the weekday name
// itself for weekly ("Monday"), or the day number as a string for monthly
// ("15"). Comparing/deriving this as one shape (instead of two separately-typed
// fields) is what keeps the dirty-check symmetric — see AGENT-653 notes in
// AgentDetail.vue / MacroDetail.vue.

export const WEEKDAY_OPTIONS = [
	{ label: "Monday", value: "Monday" },
	{ label: "Tuesday", value: "Tuesday" },
	{ label: "Wednesday", value: "Wednesday" },
	{ label: "Thursday", value: "Thursday" },
	{ label: "Friday", value: "Friday" },
	{ label: "Saturday", value: "Saturday" },
	{ label: "Sunday", value: "Sunday" },
];

export const DAY_OF_MONTH_OPTIONS = Array.from({ length: 31 }, (_, i) => {
	const n = i + 1;
	return { label: ordinal(n), value: String(n) };
});

// 1st, 2nd, 3rd, 4th ... 11th, 12th, 13th (the teens are all "th", the classic
// exception to the mod-10 rule) ... 21st, 22nd, 23rd ... 31st.
export function ordinal(n) {
	const v = Number(n);
	if (!Number.isFinite(v)) return String(n);
	const mod100 = Math.abs(v) % 100;
	if (mod100 >= 11 && mod100 <= 13) return `${v}th`;
	switch (Math.abs(v) % 10) {
		case 1:
			return `${v}st`;
		case 2:
			return `${v}nd`;
		case 3:
			return `${v}rd`;
		default:
			return `${v}th`;
	}
}

// The Day control's model value for a (frequency, weekday, day_of_month) triple
// read off a saved row — "" when the frequency doesn't use a day anchor, or when
// no anchor was ever saved for it (a legacy row, or the anchor cleared).
export function deriveScheduleDay(frequency, weekday, dayOfMonth) {
	if (frequency === "weekly") return weekday || "";
	if (frequency === "monthly") return dayOfMonth ? String(dayOfMonth) : "";
	return "";
}

// "on Monday" / "on the 15th" / "" — the anchor clause the summary line
// appends after the frequency word. Empty when there is no day to name (daily,
// or a weekly/monthly row with no anchor saved) so the legacy summary wording
// ("Scheduled monthly at 9:00 am...") is preserved verbatim for those rows.
export function scheduleAnchorPhrase(frequency, day) {
	if (!day) return "";
	if (frequency === "weekly") return `on ${day}`;
	if (frequency === "monthly") {
		const n = Number(day);
		return Number.isFinite(n) ? `on the ${ordinal(n)}` : "";
	}
	return "";
}
