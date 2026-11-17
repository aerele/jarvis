import { __ } from "@/lib/i18n";

// Pure helpers for the admin "User activity" page: the option lists the filters offer,
// the plain-English labels, and the number / time / legend formatting. No Vue here, so
// the rules are unit-testable on their own.

// The filter values the server accepts and the words the page shows for them. The
// activity list mirrors the Desk report's (jarvis_user_activity.js): a value never
// changes, only its label.
export function activityOptions() {
	return [
		{ value: "", label: __("All activity") },
		{ value: "read", label: __("Read") },
		{ value: "export", label: __("Export") },
		{ value: "action", label: __("App action") },
		{ value: "denied_call", label: __("Refused call") },
		{ value: "auth_rejected", label: __("Rejected sign-in") },
		{ value: "foreign_session", label: __("Unknown session") },
		{ value: "create", label: __("Create") },
		{ value: "update", label: __("Update") },
		{ value: "submit", label: __("Submit") },
		{ value: "cancel", label: __("Cancel") },
		{ value: "amend", label: __("Amend") },
		{ value: "delete", label: __("Delete") },
		{ value: "run", label: __("Run method") },
		{ value: "import", label: __("Import") },
		{ value: "workflow", label: __("Workflow action") },
		{ value: "email", label: __("Email") },
		{ value: "comment", label: __("Comment") },
		{ value: "share", label: __("Share") },
		{ value: "unshare", label: __("Unshare") },
		{ value: "assign", label: __("Assign") },
		{ value: "unassign", label: __("Unassign") },
		{ value: "tag", label: __("Tag") },
		{ value: "untag", label: __("Untag") },
		{ value: "attach", label: __("Attach") },
		{ value: "write", label: __("Other change") },
	];
}

export function outcomeOptions() {
	return [
		{ value: "", label: __("All outcomes") },
		{ value: "ok", label: __("OK") },
		{ value: "denied", label: __("Denied") },
		{ value: "failed", label: __("Failed") },
		{ value: "applied", label: __("Applied") },
		{ value: "refused", label: __("Refused") },
		{ value: "discarded", label: __("Discarded") },
		{ value: "partial", label: __("Partial") },
	];
}

const labelOf = (options, value) => (options.find((o) => o.value === value) || {}).label;

export function activityLabel(value) {
	return (value && labelOf(activityOptions(), value)) || value || "";
}

export function outcomeLabel(value) {
	return (value && labelOf(outcomeOptions(), value)) || value || "";
}

// frappe-ui Badge themes: a result that went through is green, one that was stopped or
// broke is red, anything in between is orange. Unknown outcomes stay neutral.
export function outcomeTheme(value) {
	if (value === "ok" || value === "applied") return "green";
	if (value === "denied" || value === "refused" || value === "failed") return "red";
	if (value === "partial" || value === "discarded") return "orange";
	return "gray";
}

// Flags that mean data may have left (or been destroyed) are red; the softer signals orange.
const RED_FLAGS = new Set(["delete_burst", "big_export", "daily_export"]);
export function flagTheme(key) {
	return RED_FLAGS.has(key) ? "red" : "orange";
}

// The Summary's number columns, in the Desk report's order. `key` is the person field.
export function summaryColumns() {
	return [
		{ key: "turns", label: __("Turns") },
		{ key: "tokens", label: __("Tokens") },
		{ key: "reads", label: __("Reads") },
		{ key: "rows_read", label: __("Records read") },
		{ key: "exports", label: __("Exports") },
		{ key: "rows_exported", label: __("Records exported") },
		{ key: "writes", label: __("Writes") },
		{ key: "erp_deletes", label: __("ERP deletes") },
		{ key: "refused_denied", label: __("Refused or denied") },
		{ key: "failed", label: __("Failed") },
		{ key: "actions", label: __("Actions") },
	];
}

export function formatCount(n) {
	const v = Number(n);
	return Number.isFinite(v) ? v.toLocaleString("en-US") : "0";
}

const MONTHS = [
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

// The server's site-local datetime ("2026-11-17 14:05:09[.123456]") as "Nov 17, 14:05".
// Deliberately NOT converted to the browser's zone: "off hours" and "a day" are the
// site's clock, so the page must read the same as the Desk report. Anything that does
// not parse is shown as it came.
export function formatWhen(value) {
	const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(String(value ?? ""));
	if (!m) return String(value ?? "");
	return `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}, ${m[4]}:${m[5]}`;
}

function pad(n) {
	return String(n).padStart(2, "0");
}

// "YYYY-MM-DD" in the browser's local calendar (the server clamps a date past its today).
export function isoDay(date) {
	return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

// The page opens on the last 30 days, as the Desk report does. `to` is left empty: the
// server then reads up to now on the site's clock, so a browser in another zone (or a tab
// left open past midnight) can never cut off today's events.
export function defaultRange(now = new Date()) {
	const from = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 30);
	return { from: isoDay(from), to: "" };
}

const DAY_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
function isRealDay(v) {
	const m = DAY_RE.exec(v);
	if (!m) return false;
	const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
	return d.getMonth() === Number(m[2]) - 1 && d.getDate() === Number(m[3]);
}

// "2026-10-01" as "Oct 1, 2026" (no timezone conversion, like formatWhen).
export function formatDay(value) {
	const m = DAY_RE.exec(String(value ?? ""));
	if (!m) return String(value ?? "");
	return `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}, ${m[1]}`;
}

// The day the oldest recorded event falls on ("" when nothing has ever been recorded).
// `first` is the server's { agent, app } pair of first-event datetimes.
export function earliestEvent(first) {
	const days = [first && first.agent, first && first.app]
		.map((v) => String(v || "").slice(0, 10))
		.filter(isRealDay)
		.sort();
	return days[0] || "";
}

// ── the URL (applied filters + tab), so a view can be linked, reloaded and stepped back to ──
export const TAB_KEYS = ["summary", "events"];
const FILTER_KEYS = ["user", "activity", "outcome"];

// Summary is the default tab and is left out. `from` is always written (even empty): an
// empty From is a choice, an absent one means "the default range".
export function queryFromState({ tab, filters }) {
	const q = {};
	if (tab && tab !== "summary") q.tab = tab;
	q.from = filters.from || "";
	if (filters.to) q.to = filters.to;
	for (const k of FILTER_KEYS) if (filters[k]) q[k] = filters[k];
	return q;
}

const one = (v) => (typeof v === "string" ? v : undefined);

// Anything that is not a valid value is ignored (the default stands in), never an error:
// a hand-edited or stale link must still open.
export function stateFromQuery(query, defaults) {
	const q = query || {};
	const tab = TAB_KEYS.includes(one(q.tab)) ? q.tab : "summary";
	const day = (v) => (one(v) === "" || isRealDay(one(v) || "") ? v : undefined);
	const from = day(q.from) ?? defaults.from;
	let to = day(q.to) ?? defaults.to;
	if (from && to && from > to) to = defaults.to;
	const activityOk = (v) => activityOptions().some((o) => o.value && o.value === v);
	const outcomeOk = (v) => outcomeOptions().some((o) => o.value && o.value === v);
	const user = one(q.user);
	return {
		tab,
		filters: {
			from,
			to,
			user: user && user.length <= 140 ? user : "",
			activity: activityOk(one(q.activity)) ? q.activity : "",
			outcome: outcomeOk(one(q.outcome)) ? q.outcome : "",
		},
	};
}

// One line per flag, from the server's own thresholds so the legend can never drift
// from what the flags actually test. Whole sentences, so a translator sees each in full.
export function legendLines(t) {
	if (!t) return [];
	const burst =
		t.delete_burst_minutes === 60
			? __("{0} or more ERP deletes by one person within 1 hour.", [
					formatCount(t.delete_burst_count),
			  ])
			: __("{0} or more ERP deletes by one person within {1} minutes.", [
					formatCount(t.delete_burst_count),
					formatCount(t.delete_burst_minutes),
			  ]);
	return [
		{ key: "delete_burst", title: __("Delete burst"), text: burst },
		{
			key: "big_export",
			title: __("Big export"),
			text: __("A single export of {0} or more records.", [formatCount(t.big_export_rows)]),
		},
		{
			key: "daily_export",
			title: __("Heavy exporting"),
			text: __("{0} or more records exported by one person in one day.", [
				formatCount(t.daily_export_rows),
			]),
		},
		{
			key: "refusals",
			title: __("Repeated refusals"),
			text: __("{0} or more refused or denied calls by one person in one day.", [
				formatCount(t.refusals_per_day),
			]),
		},
		{
			key: "off_hours",
			title: __("Off hours"),
			text: __("Activity before {0}:00 or from {1}:00, site time.", [
				pad(t.business_hours_start),
				pad(t.business_hours_end),
			]),
		},
	];
}

// The Desk report this page's Excel export lives in, opened on the same view and filters.
export function reportUrl({ view, from, to, user, activity, outcome }) {
	const q = new URLSearchParams();
	if (from) q.set("from_date", from);
	if (to) q.set("to_date", to);
	if (user) q.set("user", user);
	q.set("view", view === "events" ? "Events" : "Summary");
	// The summary ignores these two, so the report link only carries them for Events.
	if (view === "events") {
		if (activity) q.set("activity", activity);
		if (outcome) q.set("outcome", outcome);
	}
	return `/app/query-report/Jarvis%20User%20Activity?${q.toString()}`;
}
