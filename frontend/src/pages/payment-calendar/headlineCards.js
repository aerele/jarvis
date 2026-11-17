import { dayMonthLabel, monthLabel } from "@/lib/paymentCalendar/dates";
import { sign, snapshotMoney } from "@/lib/paymentCalendar/format";

// View models for the three headline cards. Amounts, states and signs are server fields:
// nothing here adds or compares money. Missing fields read as "Unavailable", never ₹0.
// `cause` on an unavailable card says why: "missing" (the server sent none) or "invalid"
// (it sent something this page cannot read). It reaches the DOM as data-cause.
const LOADING = "Loading values…";
const LOWEST_STATES = {
	safe: { state: "green", pill: "Safe" },
	tight: { state: "amber", pill: "Tight" },
	below: { state: "red", pill: "Below minimum" },
};

const isLoading = (snapshot) => !snapshot.snapshot_id;
const money = (snapshot, value, options) => snapshotMoney(snapshot).signed(value, options);

function loadingCard(extra = {}) {
	return {
		state: "unavailable",
		pill: "",
		value: "",
		note: LOADING,
		loading: true,
		actionDisabled: true,
		...extra,
	};
}

export function lowestCard(snapshot) {
	if (isLoading(snapshot)) return loadingCard({ footer: "", action: "View unavailable" });
	const horizon = snapshot.horizon;
	const window = horizon?.start && horizon?.end ? footer(horizon) : "";
	const lowest = horizon?.lowest;
	const style = LOWEST_STATES[lowest?.state];
	if (!lowest || !style || sign(lowest.cushion) === null || !lowest.date) {
		return {
			state: "unavailable",
			pill: "Incomplete",
			value: "Unavailable",
			note: snapshot.scope?.accounts?.length
				? "A complete 30-day forecast is required."
				: "Select bank and cash accounts.",
			cause: lowest ? "invalid" : "missing",
			footer: window,
			action: "View unavailable",
			actionDisabled: true,
			date: null,
		};
	}
	const day = dayMonthLabel(lowest.date);
	return {
		state: style.state,
		pill: style.pill,
		value: money(snapshot, lowest.cushion),
		note: `${sign(lowest.cushion) < 0 ? "Short of minimum on" : "Lowest dip on"} ${day}`,
		footer: window,
		action: `View ${day} →`,
		actionDisabled: false,
		date: lowest.date,
	};
}

const footer = (horizon) =>
	`${dayMonthLabel(horizon.start)}–${dayMonthLabel(horizon.end)} · 30 days`;

const MONTH_STYLES = {
	1: { state: "positive", note: "Surplus this month" },
	"-1": { state: "negative", note: "Net outflow this month" },
	0: { state: "neutral", note: "Inflows match outflows" },
};

// Zero net with zero listed rows on both sides is an empty month, not a balanced one.
const hasNoRows = (totals) => totals?.payable_rows === 0 && totals?.receivable_rows === 0;

// `month` is the displayed month. The core keeps the old snapshot while the next month loads,
// so a snapshot of another month is a loading frame: its totals never carry the new label.
export function monthCard(snapshot, month = "") {
	const held = snapshot.projection?.month || snapshot.scope?.month || "";
	const period = monthLabel(month || held);
	if (isLoading(snapshot) || (month && held && held !== month))
		return loadingCard({ period, action: "View totals →" });
	const totals = snapshot.projection?.month_totals;
	const net = totals?.net;
	const style = MONTH_STYLES[sign(net)];
	if (!style) {
		return {
			state: "unavailable",
			value: "Unavailable",
			cause: net == null ? "missing" : "invalid",
			note: "Full-month data is not loaded.",
			period,
			action: "View totals →",
			actionDisabled: true,
		};
	}
	return {
		...style,
		note: hasNoRows(totals) ? "No scheduled payments" : style.note,
		value: money(snapshot, net, { plus: true }),
		period,
		action: "View totals →",
		actionDisabled: false,
	};
}

export function immediateCard(snapshot) {
	const note = "Overdue & undated supplier invoices";
	if (isLoading(snapshot)) return loadingCard({ action: "View invoices →" });
	const value = snapshot.projection?.due_now?.immediate;
	const amount = sign(value);
	if (amount === null || amount < 0) {
		return {
			state: "unavailable",
			pill: "",
			value: "Unavailable",
			cause: value == null ? "missing" : "invalid",
			note,
			action: "View invoices →",
			actionDisabled: true,
		};
	}
	return {
		state: amount > 0 ? "amber" : "green",
		pill: amount > 0 ? "Needs review" : "Clear",
		value: money(snapshot, value),
		note,
		action: "View invoices →",
		actionDisabled: false,
	};
}
