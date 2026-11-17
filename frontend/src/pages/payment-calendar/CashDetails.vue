<template>
	<details
		id="financial-details"
		class="financial-details"
		:open="open"
		@toggle="emit('update:open', $event.target.open)"
	>
		<summary>
			<span>Selected date &amp; cash details</span
			><span id="details-summary-date">{{ fullDateLabel(selected) }}</span>
		</summary>
		<div class="overview-grid">
			<section class="snapshot-card" aria-label="Current snapshot balances">
				<div class="snapshot-heading">
					<h2>Current balances</h2>
					<span>As of {{ snapshotStamp(snapshot.generated_at, timezone) }}</span>
				</div>
				<div class="book-cash-row">
					<span>Book cash now</span>
					<button
						id="book-cash"
						type="button"
						class="book-cash-value"
						aria-label="View bank and cash account balance breakdown"
						@click="emit('balances')"
					>
						{{ money(projection.book_cash) }}
					</button>
				</div>
				<div class="payable-now-row">
					<span>Total payable now</span>
					<button
						id="total-payable"
						type="button"
						class="snapshot-total"
						data-obligation="now"
						aria-label="View all invoices in total payable now"
						:disabled="!hasValue(dueNow.total)"
						@click="emit('obligation', 'now')"
					>
						{{ money(dueNow.total) }} <span aria-hidden="true">→</span>
					</button>
				</div>
				<div class="obligation-breakdown">
					<button
						v-for="row in obligations"
						:key="row.scope"
						type="button"
						:data-obligation="row.scope"
						:disabled="!hasValue(row.value)"
						@click="emit('obligation', row.scope)"
					>
						<span>{{ row.label }}</span
						><b>{{ money(row.value) }} <span aria-hidden="true">→</span></b>
					</button>
				</div>
				<button
					id="balances-button"
					type="button"
					class="snapshot-balance-link"
					@click="emit('balance-details')"
				>
					View balance details <span aria-hidden="true">→</span>
				</button>
			</section>
			<section class="dated-overview" aria-labelledby="selected-date-heading">
				<div class="dated-heading">
					<div>
						<h2 id="selected-date-heading">{{ heading }}</h2>
						<p id="scope-caption">{{ caption }}</p>
					</div>
					<div class="dated-actions">
						<div class="segmented" role="group" aria-label="Summary scope">
							<button
								v-for="choice in SCOPES"
								:id="`scope-${choice.value}`"
								:key="choice.value"
								type="button"
								:class="{ active: scope === choice.value }"
								:aria-pressed="String(scope === choice.value)"
								@click="emit('update:scope', choice.value)"
							>
								{{ choice.label }}
							</button>
						</div>
						<button
							id="selected-invoices"
							type="button"
							class="pcal-primary-btn"
							:disabled="invoicesDisabled"
							@click="emit('invoices', undefined)"
						>
							View invoices<ArrowIcon />
						</button>
					</div>
				</div>
				<div class="selected-stats">
					<article class="stat">
						<div id="payable-label" class="stat-label">{{ payableLabel }}</div>
						<button
							id="stat-payable"
							type="button"
							class="stat-value amount-action"
							:class="{ skeleton: figures.loading }"
							aria-describedby="payable-label"
							:disabled="figures.payable === null"
							@click="emit('invoices', 'Payable')"
						>
							{{ figures.loading ? "" : money(figures.payable) }}
						</button>
						<div class="stat-note">
							Supplier invoices <span aria-hidden="true">→</span>
						</div>
					</article>
					<article class="stat">
						<div id="receivable-label" class="stat-label">{{ receivableLabel }}</div>
						<button
							id="stat-receivable"
							type="button"
							class="stat-value amount-action"
							:class="{ skeleton: figures.loading }"
							aria-describedby="receivable-label"
							:disabled="figures.receivable === null"
							@click="emit('invoices', 'Receivable')"
						>
							{{ figures.loading ? "" : money(figures.receivable) }}
						</button>
						<div class="stat-note">
							Customer invoices · not cash received <span aria-hidden="true">→</span>
						</div>
					</article>
					<article class="stat closing">
						<div id="closing-label" class="stat-label">
							{{
								scope === "month"
									? "Projected month-end closing"
									: "Projected closing"
							}}
						</div>
						<div
							id="stat-closing"
							class="stat-value"
							:class="{ unavailable: !forecast, skeleton: figures.loading }"
							:data-tone="figures.tone"
						>
							{{ closing }}
						</div>
						<p id="closing-note" class="forecast-assumption">{{ closingNote }}</p>
						<strong
							id="floor-margin"
							class="floor-margin"
							:class="{ below: marginBelow }"
							>{{ forecast ? figures.marginText : "" }}</strong
						>
						<div id="floor-context" class="stat-note">
							{{ forecast ? figures.context : "" }}
						</div>
					</article>
				</div>
				<div v-if="riskDate" id="risk-alert" class="risk-alert" role="status">
					<span aria-hidden="true">!</span>
					<span id="risk-message"
						>Cash balance falls below the minimum on
						{{ dayMonthLabel(riskDate) }}.</span
					>
					<button id="risk-jump" type="button" @click="jumpToRisk">
						View {{ dayMonthLabel(riskDate) }}
					</button>
				</div>
			</section>
		</div>
	</details>
</template>
<script setup>
import { computed } from "vue";
import {
	dayMonthLabel,
	fullDateLabel,
	monthLabel,
	snapshotStamp,
} from "@/lib/paymentCalendar/dates";
import { sign } from "@/lib/paymentCalendar/format";
import ArrowIcon from "./ArrowIcon.vue";
import { CashFigures } from "./cashFigures";
import "./tokens.css";
import "./CashDetails.css";

const props = defineProps({
	snapshot: { type: Object, required: true },
	selected: { type: String, required: true },
	// The displayed month (YYYY-MM): the risk alert and the month scope belong to it.
	month: { type: String, required: true },
	scope: { type: String, default: "day" },
	// Owned by the page: the panel is unmounted while a new snapshot loads, and must come back as it was.
	open: { type: Boolean, default: false },
	timezone: { type: String, default: "" },
});
const emit = defineEmits([
	"update:scope",
	"update:open",
	"balances",
	"balance-details",
	"obligation",
	"invoices",
	"jump",
]);

const SCOPES = [
	{ value: "day", label: "Day view" },
	{ value: "month", label: "Monthly aggregate" },
];
const CAPTIONS = {
	day: "Day view · Scheduled amounts on this date",
	month: "Monthly aggregate · All dated remaining balances",
};

const projection = computed(() => props.snapshot.projection || {});
const dueNow = computed(() => projection.value.due_now || {});
const figures = computed(
	() => new CashFigures(props.snapshot, props.selected, props.scope, props.month)
);
const forecast = computed(() => figures.value.forecast);
const marginBelow = computed(() => !!forecast.value && figures.value.marginBelow);

const money = (value) => figures.value.money(value);
const hasValue = (value) => typeof value === "string";

const anchor = computed(() => props.snapshot.scope?.anchor || "");
const obligations = computed(() => [
	{
		scope: "today",
		label: `Due today · ${dayMonthLabel(anchor.value)}`,
		value: dueNow.value.scheduled,
	},
	{ scope: "overdue", label: "Overdue", value: dueNow.value.overdue },
	{ scope: "undated", label: "No due date", value: dueNow.value.undated },
]);

const isMonth = computed(() => props.scope === "month");
const monthName = computed(() => monthLabel(props.month));
const heading = computed(() => (isMonth.value ? monthName.value : fullDateLabel(props.selected)));
const caption = computed(() => CAPTIONS[props.scope] || CAPTIONS.day);
const periodLabel = (verb) =>
	isMonth.value
		? `${verb} in ${monthName.value.split(" ")[0]}`
		: `${verb} on ${dayMonthLabel(props.selected)}`;
const payableLabel = computed(() => periodLabel("Due"));
const receivableLabel = computed(() => periodLabel("Expected"));
const invoicesDisabled = computed(() => !figures.value.hasInvoices);

const closing = computed(() =>
	figures.value.loading
		? ""
		: forecast.value
		? money(forecast.value.projected_closing)
		: "No forecast available"
);
const closingNote = computed(() =>
	forecast.value ? figures.value.collectionsNote : figures.value.reason
);

// The template rule: the first date of the displayed month, not before the snapshot day,
// whose closing is below the minimum. Read from the month's per-day server margins (the
// 30-day horizon cannot see a later month); a missing margin never alerts.
const riskDate = computed(() => {
	if (figures.value.loading) return "";
	const days = props.snapshot.projection?.days || {};
	const inMonth = (date) => date.startsWith(props.month + "-") && date >= anchor.value;
	return (
		Object.keys(days)
			.sort()
			.find((date) => inMonth(date) && sign(days[date]?.margin) < 0) || ""
	);
});
function jumpToRisk() {
	emit("update:scope", "day");
	emit("jump", riskDate.value);
}
</script>
