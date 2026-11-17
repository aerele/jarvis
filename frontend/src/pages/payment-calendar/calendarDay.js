// Display model for one calendar date. The server owns every figure; this file only
// inspects decimal strings (never parses or adds them), formats them and picks copy.
import { dayMonthLabel, fullDateLabel, isSelectable } from "@/lib/paymentCalendar/dates";
import { netLabel, shortMoney, sign, snapshotMoney } from "@/lib/paymentCalendar/format";

// Side names match the composable's "Payable" / "Receivable": keep in sync with SIDES in
// usePaymentLedger.js.
const SIDES = [
	{ side: "Payable", label: "Pay", field: "payable_remaining" },
	{ side: "Receivable", label: "Receive", field: "receivable_remaining" },
];

// Template status words; the server's "Paid"/"Settled" and both "Partially" forms collapse.
const STATUS = {
	Paid: { text: "Settled", kind: "settled" },
	Settled: { text: "Settled", kind: "settled" },
	"Partially paid": { text: "Partial", kind: "partial" },
	"Partially settled": { text: "Partial", kind: "partial" },
	Unpaid: { text: "Unpaid", kind: "unpaid" },
	"No dues": { text: "No dues", kind: "none" },
};

export const hasAmount = (value) => !!sign(value);

export const hasActivity = (entry) =>
	!!entry && (hasAmount(entry.payable_remaining) || hasAmount(entry.receivable_remaining));

export function netSign(net) {
	const direction = sign(net);
	if (!direction) return "zero";
	return direction < 0 ? "negative" : "positive";
}

// Builds the view of every date from the props the grid already has.
export class DayViews {
	constructor({ snapshot, month, loading, loadError }) {
		this.snapshot = snapshot;
		this.month = month;
		this.loading = loading;
		this.loadError = loadError;
		this.scope = snapshot.scope;
		this.projection = snapshot.projection;
		this.currency = snapshot.currency;
		this.money = snapshotMoney(snapshot);
	}

	view(date) {
		const entry = this.projection.days[date];
		const selectable = isSelectable(date, this.scope);
		const exact = (value) => this.exact(value);
		const star = this.star(entry);
		const base = {
			date,
			entry,
			selectable,
			today: date === this.scope.anchor,
			adjacent: !date.startsWith(this.month),
			short: dayMonthLabel(date),
			star,
			sides: SIDES.map((spec) => this.side(spec, entry)),
			status: selectable ? this.status(date, entry) : null,
			net: this.net(entry),
		};
		base.dash = base.net ? "" : this.dash(entry);
		base.activity = hasActivity(entry);
		base.line = this.line(base);
		base.listNet = {
			text: netLabel(entry?.net, this.currency),
			negative: netSign(entry?.net) === "negative",
		};
		base.label = [
			fullDateLabel(date),
			entry
				? `payable ${exact(entry.payable_remaining)}, receivable ${exact(
						entry.receivable_remaining
				  )}`
				: "data unavailable",
			star ? "one invoice exceeds half of supplier dues" : "",
		]
			.filter(Boolean)
			.join(", ");
		return base;
	}

	// List sub-line: "Today · Partial", or why the date has no status.
	line({ today, status }) {
		return (today ? "Today · " : "") + (status?.text ?? "Data unavailable");
	}

	// Accessible amount: exact, or "unavailable" for a null / malformed server value.
	exact(value) {
		const text = value == null ? "—" : this.money.exact(value);
		return text === "—" ? "unavailable" : text;
	}

	side({ side, label, field }, entry) {
		const value = entry?.[field];
		return {
			side,
			label,
			short: shortMoney(value, this.currency),
			accessible: this.exact(value),
		};
	}

	// Star title, e.g. "PINV-2026-0207: ₹1,02,236.00 · 100% of this date's supplier dues".
	star(entry) {
		const top = entry?.payable_concentrated === true ? entry.payable_top_invoice : null;
		if (!top?.voucher_no) return "";
		const amount = this.money.exact(top.remaining);
		const pct = entry.payable_concentration_pct;
		// No "NaN%": the share is shown only when the server sent a plain decimal.
		const share =
			typeof pct === "string" && /^\d+(\.\d+)?$/.test(pct)
				? ` · ${Number(pct).toFixed(0)}% of this date's supplier dues`
				: "";
		return `${top.voucher_no}: ${amount}${share}`;
	}

	// The net comes from the server per day; a missing or null net is never computed.
	net(entry) {
		if (!hasActivity(entry) || entry.net == null) return null;
		return {
			text: `Net ${netLabel(entry.net, this.currency)}`,
			sign: netSign(entry.net),
			title: `Scheduled receipts minus scheduled payments: ${this.exact(
				entry.net
			)}. This is not the closing cash balance.`,
		};
	}

	dash(entry) {
		if (!entry) return this.loading ? "Loading values" : "Data unavailable";
		return entry.net == null ? "Data unavailable" : "No scheduled net flow";
	}

	status(date, entry) {
		const text = this.statusText(date, entry);
		// A date with no data at all shows only the quiet dash.
		if (!entry && text === "Unavailable") return null;
		return Object.hasOwn(STATUS, text) ? STATUS[text] : { text, kind: "neutral" };
	}

	statusText(date, entry) {
		const { projection, snapshot, scope, month } = this;
		if ((this.loading || this.loadError) && !entry && date.startsWith(month + "-"))
			return this.loadError ? "Not loaded" : "Loading…";
		if (scope.month && !date.startsWith(scope.month + "-")) return "Load month";
		const certified =
			projection.payable_settlement_complete === true &&
			projection.reconciliation?.Payable != null &&
			!snapshot.unavailable?.Payable;
		return certified &&
			typeof entry?.settlement === "string" &&
			Object.hasOwn(STATUS, entry.settlement)
			? entry.settlement
			: "Unavailable";
	}
}
