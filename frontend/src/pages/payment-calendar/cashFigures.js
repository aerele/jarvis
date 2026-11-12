import { dayMonthLabel } from "@/lib/paymentCalendar/dates";
import { sign, snapshotMoney } from "@/lib/paymentCalendar/format";
import { explain } from "./messages";

// View model of the "Selected date & cash details" figures and of the calculation example.
// Every amount, margin and closing is a server string: this class picks the day, compares
// dates as strings, reads the sign of a server string and chooses copy. It adds nothing.
const NO_ACCOUNTS_REASON = "Select a bank or cash account to see the forecast.";
const LOADING_REASON = "Loading values…";
const NO_FORECAST_REASON = "The forecast is unavailable for this date.";

export class CashFigures {
	// scope: "day" or "month". A month reads its month-end day. `month` is the displayed
	// month: the core keeps the old snapshot while the next month loads, so a snapshot of
	// another month counts as loading. An empty `month` skips that check.
	constructor(snapshot, selected, scope, month = "") {
		this.snapshot = snapshot;
		this.selected = selected;
		this.isMonth = scope === "month";
		this.month = month;
	}

	get projection() {
		return this.snapshot.projection || {};
	}

	get anchor() {
		return this.snapshot.scope?.anchor || "";
	}

	get loading() {
		const held = this.snapshot.scope?.month;
		return !this.snapshot.snapshot_id || (!!this.month && !!held && held !== this.month);
	}

	// The date the closing and the calculation example read; none while loading, so a stale
	// snapshot's month end never shows.
	get date() {
		if (this.loading) return "";
		return this.isMonth
			? this.snapshot.scope?.window_end || this.projection.end || ""
			: this.selected;
	}

	get day() {
		return this.projection.days?.[this.date] || null;
	}

	get dayStats() {
		return this.projection.days?.[this.selected] || null;
	}

	get totals() {
		return this.projection.month_totals || null;
	}

	get payable() {
		if (this.loading) return null;
		return this.isMonth
			? this.totals?.payable ?? null
			: this.dayStats?.payable_remaining ?? null;
	}

	get receivable() {
		if (this.loading) return null;
		return this.isMonth
			? this.totals?.receivable ?? null
			: this.dayStats?.receivable_remaining ?? null;
	}

	// "View invoices" needs invoice data for the scope: a complete month, or a loaded day.
	get hasInvoices() {
		if (this.loading) return false;
		return this.isMonth ? this.totals?.complete === true : !!this.dayStats;
	}

	// The forecast of `date`, present only when the server computed every field of it.
	get forecast() {
		const day = this.loading ? null : this.day;
		const fields = day
			? [
					day.projected_closing,
					day.margin,
					day.cumulative_receivable,
					day.cumulative_payable,
			  ]
			: [];
		return fields.length && fields.every((value) => value != null) ? day : null;
	}

	// muted | negative | caution | positive: from the signs of the server's closing and margin.
	get tone() {
		const day = this.forecast;
		if (!day) return "muted";
		if (sign(day.projected_closing) < 0) return "negative";
		return sign(day.margin) < 0 ? "caution" : "positive";
	}

	get collectionsNote() {
		const label = this.date === this.anchor ? "today" : "by " + dayMonthLabel(this.date);
		return `Assumes ${this.money(this.forecast.cumulative_receivable)} is collected ${label}.`;
	}

	// "₹2,13,552.00 above minimum": the sign of the server's margin picks the word.
	get marginText() {
		const margin = this.forecast.margin;
		const below = sign(margin) < 0;
		return `${this.money(margin.replace(/^-/, ""))} ${below ? "below" : "above"} minimum`;
	}

	get marginBelow() {
		return sign(this.forecast?.margin) < 0;
	}

	get context() {
		return `Minimum ${this.money(this.projection.floor)} · ${dayMonthLabel(this.date)}`;
	}

	// Why there is no forecast for `date`, in the order the template checks.
	get reason() {
		if (this.loading) return LOADING_REASON;
		const scope = this.snapshot.scope || {};
		if (this.date < this.anchor)
			return `Forecast starts on ${dayMonthLabel(
				this.anchor
			)}. Past dates show current remaining dues.`;
		if (!scope.accounts?.length) return NO_ACCOUNTS_REASON;
		if (scope.selectable_end && this.date > scope.selectable_end)
			return `Forecast data is loaded through ${dayMonthLabel(
				scope.selectable_end
			)}. Later dates are unavailable.`;
		const code =
			this.projection.scope_reasons?.[0] ||
			Object.values(this.snapshot.unavailable || {})[0];
		return code ? explain(code) : NO_FORECAST_REASON;
	}

	money(value, options) {
		return snapshotMoney(this.snapshot).signed(value, options);
	}
}
