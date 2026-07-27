// Shared server-datetime rendering. Frappe returns naive datetime strings in
// the SITE timezone; parsing them with new Date()/dayjs() treats them as
// browser-local and shows future-tense "in N minutes" for any viewer whose
// timezone differs from the site's. frappe-ui's dayjsLocal() parses in
// setConfig("systemTimezone") (fed from get_chat_ui_settings.time_zone in
// AppShell) and converts to the browser zone; without the config it falls
// back to plain dayjs() - today's behavior.
import { dayjsLocal, dayjs, getConfig } from "frappe-ui";

export function timeAgo(d) {
	return d ? dayjsLocal(String(d)).fromNow() : "";
}

export function exactDate(d) {
	return d ? dayjsLocal(String(d)).format("ddd, MMM D, YYYY h:mm A") : "";
}

export function formatDate(d, fmt) {
	return d ? dayjsLocal(String(d)).format(fmt || "MMM D, YYYY") : "";
}

// Epoch ms for a naive site-tz datetime string (SLA deadlines, creation), tz-aware
// via dayjsLocal — so the duration/ordering math in lib/supportSla is correct for
// any viewer's timezone, not just the site's. null for empty/invalid input.
export function toLocalMs(d) {
	if (d == null || d === "") return null;
	const dj = dayjsLocal(String(d));
	return dj && typeof dj.isValid === "function" && dj.isValid() ? dj.valueOf() : null;
}

// The send-side mirror of dayjsLocal: a browser-local datetime (an
// <input type="datetime-local"> value, "YYYY-MM-DDTHH:mm") → the naive
// site-timezone "YYYY-MM-DD HH:mm:ss" string Frappe endpoints expect.
// frappe-ui 0.1.278 defines dayjsSystem but does NOT export it from its
// index, so the two-hop tz conversion is replicated here with the exported
// `dayjs` (tz plugin already extended) + `getConfig`. Without the
// systemTimezone config it degrades to a plain reformat - today's dayjsLocal
// fallback behavior.
export function toSiteDatetime(d) {
	if (!d) return "";
	const s = String(d).replace("T", " ");
	const site = getConfig("systemTimezone");
	if (!site) return dayjs(s).format("YYYY-MM-DD HH:mm:ss");
	const local = getConfig("localTimezone") || Intl.DateTimeFormat().resolvedOptions().timeZone;
	return dayjs.tz(s, local).tz(site).format("YYYY-MM-DD HH:mm:ss");
}

// Day-bucket label for chat day separators, timezone-safe like the rest of this
// module. Accepts a naive site-tz string (creation/modified) or a browser Date /
// ms number (optimistic rows). "Today" / "Yesterday" / weekday within a week /
// "D MMMM" beyond.
export function dayLabel(d) {
	if (d == null || d === "") return "";
	const dj = dayjsLocal(d);
	if (!dj || typeof dj.isValid !== "function" || !dj.isValid()) return "";
	const diff = dayjsLocal().startOf("day").diff(dj.startOf("day"), "day");
	if (diff === 0) return "Today";
	if (diff === 1) return "Yesterday";
	if (diff > 1 && diff < 7) return dj.format("dddd");
	return dj.format("D MMMM");
}
