<template>
	<details class="method">
		<summary>How balances are calculated</summary>
		<p>
			Net in a date cell means scheduled receipts minus scheduled payments. It is not the
			bank balance.
		</p>
		<p>
			Projected cash assumes collections arrive on time. It includes overdue and undated
			supplier dues once today. The forecast starts on the snapshot date.
		</p>
	</details>
	<PcalModal
		v-model="calculationOpen"
		:title="calculationTitle"
		close-label="Close calculation example"
		size="sm"
		layer="aux"
	>
		<div class="method-body">
			<template v-if="calculation">
				<p class="method-context">Snapshot {{ stamp }}</p>
				<div v-for="row in calculation" :key="row.label" class="balance-row">
					<span>{{ row.label }}</span
					><b>{{ row.amount }}</b>
				</div>
			</template>
			<template v-else>
				<p>No forecast available.</p>
				<p>{{ figures.reason }}</p>
			</template>
		</div>
	</PcalModal>
	<PcalModal
		v-model="balanceDetailsOpen"
		title="Outstanding balance details"
		close-label="Close balance details"
		size="sm"
		layer="aux"
	>
		<div class="method-body">
			<p class="method-context">Snapshot {{ stamp }}</p>
			<div class="overflow-x-auto">
				<table class="balance-table">
					<thead>
						<tr>
							<th>Category</th>
							<th>Payable</th>
							<th>Receivable</th>
						</tr>
					</thead>
					<tbody>
						<tr v-for="row in categories" :key="row.key">
							<td>{{ row.label }}</td>
							<td v-for="side in SIDES" :key="side">{{ row.amounts[side] }}</td>
						</tr>
					</tbody>
				</table>
			</div>
			<p class="demo-note">
				Overdue and undated customer balances are outside assumed on-time collections.
			</p>
		</div>
	</PcalModal>
</template>
<script setup>
import { computed, ref } from "vue";
import { dayMonthLabel, monthLabel, snapshotStamp } from "@/lib/paymentCalendar/dates";
import { sign } from "@/lib/paymentCalendar/format";
import { CashFigures } from "./cashFigures";
import PcalModal from "./PcalModal.vue";
import "./tokens.css";
import "./CalendarMethod.css";

const props = defineProps({
	snapshot: { type: Object, required: true },
	selected: { type: String, required: true },
	scope: { type: String, default: "day" },
	// The displayed month; a snapshot of another month reads as loading. Empty skips the check.
	month: { type: String, default: "" },
	timezone: { type: String, default: "" },
});

// Keep in sync with SIDES in usePaymentLedger.js.
const SIDES = ["Payable", "Receivable"];
const calculationOpen = ref(false),
	balanceDetailsOpen = ref(false);
defineExpose({
	openCalculation: () => (calculationOpen.value = true),
	openBalanceDetails: () => (balanceDetailsOpen.value = true),
});

const projection = computed(() => props.snapshot.projection || {});
const figures = computed(
	() => new CashFigures(props.snapshot, props.selected, props.scope, props.month)
);
const calculationTitle = computed(() =>
	figures.value.date
		? `Forecast calculation · ${dayMonthLabel(figures.value.date)}`
		: "Forecast calculation"
);
const stamp = computed(() => snapshotStamp(props.snapshot.generated_at, props.timezone));
const money = (value) => figures.value.money(value);

// The calculation example: every row is a server field of the scope date; null without a forecast.
const calculation = computed(() => {
	const day = figures.value.forecast;
	if (!day) return null;
	const date = dayMonthLabel(figures.value.date);
	const dueNow = projection.value.due_now || {};
	return [
		["Book cash", projection.value.book_cash],
		[`+ Assumed collections through ${date}`, day.cumulative_receivable],
		[`− Scheduled supplier dues through ${date}`, day.cumulative_payable],
		["− Overdue supplier dues once", dueNow.overdue],
		["− Supplier dues with no date once", dueNow.undated],
		["Projected closing cash", day.projected_closing],
		["Minimum cash balance", projection.value.floor],
		["Margin above minimum", day.margin],
	].map(([label, value]) => ({ label, amount: money(value) }));
});

// Rows of the balance details: the template's four, plus the server's later-month and
// adjustment buckets when they hold something, so the rows add up to the total.
const hasBalance = (key) =>
	SIDES.some((side) => [-1, 1].includes(sign(projection.value.reconciliation?.[side]?.[key])));
const categories = computed(() => {
	// "After this month" names the held snapshot's month: neutral while another month loads.
	const named = props.snapshot.scope?.month && !figures.value.loading;
	const later = named ? "After this month" : "Outside forecast";
	// "Today" only means something in the anchor month; another month is named.
	const otherMonth =
		named && props.snapshot.scope.month !== props.snapshot.scope.anchor?.slice(0, 7);
	const first = otherMonth
		? `Due in ${monthLabel(props.snapshot.scope.month)}`
		: "Due today & later";
	const rows = [
		["current_future", first, true],
		["overdue", "Overdue", true],
		["undated", "No due date", true],
		["outside_horizon", later, hasBalance("outside_horizon")],
		["adjustments", "Adjustments", hasBalance("adjustments")],
		["canonical_total", "Total outstanding", true],
	];
	const table = rows
		.filter(([, , shown]) => shown)
		.map(([key, label]) => ({
			key,
			label,
			amounts: Object.fromEntries(
				SIDES.map((side) => [side, money(projection.value.reconciliation?.[side]?.[key])])
			),
		}));
	return hasExchangeDifference() ? [...table, exchangeRow()] : table;
});

// Foreign-currency residue the server left out of every bucket (source_notes). It is shown
// after the total, because it is not part of it.
const residue = (side) => projection.value.source_notes?.[side]?.currency_residue_excluded;
const hasExchangeDifference = () => SIDES.some((side) => residue(side)?.count);
const exchangeRow = () => ({
	key: "exchange_differences",
	label: "Exchange differences (excluded)",
	amounts: Object.fromEntries(
		SIDES.map((side) => [
			side,
			money(projection.value.reconciliation?.[side] ? residue(side)?.amount ?? null : null),
		])
	),
});
</script>
