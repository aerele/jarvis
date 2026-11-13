<template>
	<section
		class="next-panel pcal-next"
		aria-labelledby="next-payments-heading"
		:aria-busy="loading"
	>
		<div class="aside-heading">
			<h2 id="next-payments-heading">Next payments</h2>
			<span id="upcoming-month">{{ monthName }}</span>
		</div>
		<div class="next-list">
			<button
				v-for="row in visible"
				:key="row.date"
				type="button"
				class="upcoming"
				:data-upcoming="row.date"
				@click="emit('select', row.date)"
			>
				<span class="upcoming-date"
					>{{ dayMonthLabel(row.date) }}<small>{{ recordCount(row) }}</small></span
				><span class="upcoming-amount">{{ money(row.remaining) }}</span>
			</button>
			<p v-if="!visible.length" class="schedule-empty">{{ emptyText }}</p>
		</div>
		<button
			id="all-payments"
			type="button"
			class="aside-more"
			:disabled="!visible.length"
			@click="emit('view-all')"
		>
			View remaining dates<ArrowIcon />
		</button>
	</section>
</template>
<script setup>
import { computed } from "vue";
import { dayMonthLabel, monthLabel } from "@/lib/paymentCalendar/dates";
import { sign } from "@/lib/paymentCalendar/format";
import ArrowIcon from "./ArrowIcon.vue";
import { CashFigures } from "./cashFigures";
import "./tokens.css";
import "./NextPayments.css";

const props = defineProps({
	snapshot: { type: Object, required: true },
	selected: { type: String, required: true },
	// The displayed month (YYYY-MM): rows never come from another month.
	month: { type: String, required: true },
	rows: { type: Array, default: () => [] },
	loading: Boolean,
	loadError: Boolean,
});
const emit = defineEmits(["select", "view-all"]);

const MAX_ROWS = 3;
const monthName = computed(() => monthLabel(props.month));
const figures = computed(
	() => new CashFigures(props.snapshot, props.selected, "day", props.month)
);
// The core keeps the previous month's snapshot while the next one loads: that is loading,
// not an unavailable schedule.
const stale = computed(() => figures.value.loading);
const unavailable = computed(
	() =>
		!!props.snapshot.unavailable?.Payable ||
		props.snapshot.projection?.month_totals?.complete === false
);
// Dates after the selected one, in the displayed month, that pay something. The server
// sends them in date order; a date is only compared as a string.
const visible = computed(() =>
	stale.value || unavailable.value || props.loadError
		? []
		: props.rows
				.filter(
					(row) =>
						row.date > props.selected &&
						row.date.startsWith(props.month + "-") &&
						sign(row.remaining) > 0
				)
				.slice(0, MAX_ROWS)
);
const emptyText = computed(() => {
	if (props.loadError) return "Payments will appear after this month is loaded.";
	if (stale.value) return "Loading upcoming payments…";
	if (unavailable.value) return `Payment schedule unavailable for ${monthName.value}.`;
	if (props.loading) return "Loading upcoming payments…";
	return `No payments scheduled after this date in ${monthName.value}.`;
});

const recordCount = (row) => `${row.rows} ${row.rows === 1 ? "record" : "records"}`;
const money = (value) => figures.value.money(value);
</script>
