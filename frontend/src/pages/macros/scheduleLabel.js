// What a macro's schedule reads as in a list: "Daily · 9:00 am", "Weekly on Monday
// · 9:00 am", "Once · Mon, 19 Oct 2026 · 9:00 am". One implementation for the
// owner's Macros list and the admin's.
import { dayjs } from "frappe-ui";
import { formatTime12h } from "@/utils/datetime";
import { deriveScheduleDay, scheduleAnchorPhrase } from "@/lib/scheduleAnchor";

export function scheduleLabel(row) {
	const freq = row.schedule_frequency || "scheduled";
	let label = freq.charAt(0).toUpperCase() + freq.slice(1);
	// A one-time schedule names its date: "Once · Mon, 19 Oct 2026 · 9:00 am". The
	// date is the stored one, read as a calendar day (no timezone shift), like the time.
	if (freq === "once" && row.schedule_date) {
		label = `${label} · ${dayjs(row.schedule_date).format("ddd, D MMM YYYY")}`;
	}
	// jarvis#653: "Weekly on Monday" / "Monthly on the 15th" when the row has an
	// anchor saved; unchanged ("Weekly") for a legacy row with none.
	const day = deriveScheduleDay(freq, row.schedule_weekday, row.schedule_day_of_month);
	const anchor = scheduleAnchorPhrase(freq, day);
	if (anchor) label = `${label} ${anchor}`;
	// The same 12-hour text the macro's form and its time picker show ("9:00 am");
	// this cell used to print the stored 24-hour value ("09:00").
	const t = formatTime12h(row.schedule_time);
	return t ? `${label} · ${t}` : label;
}
