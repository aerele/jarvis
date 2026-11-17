import { siteTimezone } from "@/utils/datetime";
import {
	dayMonthLabel,
	fullDateLabel,
	monthLabel,
	snapshotStamp,
} from "@/lib/paymentCalendar/dates";
import { snapshotMoney } from "@/lib/paymentCalendar/format";

// Keep in sync with SIDES in usePaymentLedger.js.
const SIDES = [
	{ side: "Payable", label: "Payables", rows: "payable_rows", amount: "payable_remaining" },
	{
		side: "Receivable",
		label: "Receivables",
		rows: "receivable_rows",
		amount: "receivable_remaining",
	},
];
const TABBED_SCOPES = ["date", "month"];

// The header text of the side panel and of the full-details dialog (template scopeTitle).
export function scopeTitle(scope, { date, anchor }) {
	return (
		{
			date: () => fullDateLabel(date),
			immediate: () => "Immediate dues · Overdue & undated",
			now: () => "Total payable now",
			today: () => "Due today · " + dayMonthLabel(anchor),
			overdue: () => "Overdue supplier invoices",
			undated: () => "Supplier invoices with no due date",
			month: () => monthLabel(date.slice(0, 7)) + " · Invoices",
		}[scope]?.() ?? ""
	);
}

// Counts and totals are server fields; a missing one is "unknown", never zero.
function serverFigures(snapshot, scope, date, spec) {
	if (snapshot.unavailable?.[spec.side]) return null;
	if (scope === "month") {
		const totals = snapshot.projection?.month_totals;
		const side = spec.side === "Payable" ? "payable" : "receivable";
		return totals ? { count: totals[side + "_rows"], amount: totals[side] } : null;
	}
	const day = snapshot.projection?.days?.[date];
	return day ? { count: day[spec.rows], amount: day[spec.amount] } : null;
}

// The two tabs of the "Invoice side" group; empty for the obligation scopes.
export function sideTabs(snapshot, scope, date) {
	if (!TABBED_SCOPES.includes(scope)) return [];
	return SIDES.map((spec) => {
		const figures = serverFigures(snapshot, scope, date, spec);
		const known = Number.isInteger(figures?.count) && typeof figures?.amount === "string";
		return {
			side: spec.side,
			label: spec.label,
			known,
			count: known ? String(figures.count) : "—",
			total: known ? snapshotMoney(snapshot).exact(figures.amount) : "Unavailable",
		};
	});
}

// "11 Nov"; the year is added only when it differs from the snapshot's, so an old overdue
// invoice is not mistaken for a recent one.
export function dueLabel(due, anchor) {
	if (!due) return "No due date";
	const label = dayMonthLabel(due);
	return due.slice(0, 4) === anchor.slice(0, 4) ? label : `${label} ${due.slice(0, 4)}`;
}

const stampOf = (generatedAt) => snapshotStamp(generatedAt, siteTimezone());

// Under the heading of the side panel.
export const panelSubline = (scope, generatedAt) =>
	scope === "date" ? "Current remaining balances" : "Snapshot " + stampOf(generatedAt);

// Under the heading of the full-details dialog.
export const dialogContext = (scope, generatedAt) =>
	(TABBED_SCOPES.includes(scope) ? "Remaining balances as of " : "Snapshot ") +
	stampOf(generatedAt);
