<template>
	<div
		class="pcal-kpis"
		:class="{ 'long-values': cards.some((card) => card.value.length > 14) }"
	>
		<article
			v-for="card in cards"
			:key="card.key"
			:data-kpi="card.key"
			class="pcal-kpi"
			:class="{ wide: card.value.length > 14 }"
		>
			<p class="kpi-label">{{ card.label }}</p>
			<p class="kpi-value" :class="card.key === 'closing' ? riskClass : ''">
				{{ card.value }}
			</p>
			<p class="kpi-note">{{ card.note }}</p>
			<p v-if="card.key === 'closing'" class="kpi-note kpi-secondary">
				Opening {{ money(day?.projected_opening) }}<br />Minimum
				{{ money(snapshot.projection.floor) }}
			</p>
			<p
				v-if="card.key === 'payable' && selected === snapshot.scope.anchor"
				class="kpi-note kpi-secondary"
			>
				Payable now {{ money(snapshot.projection.due_now.total) }}<br />
				Overdue {{ money(snapshot.projection.due_now.overdue) }}<br />
				Undated {{ money(snapshot.projection.due_now.undated) }}
			</p>
		</article>
	</div>
</template>
<script setup>
import { computed } from "vue";
import { exactMoney } from "@/lib/paymentCalendar/format";
import { dateLabel } from "@/lib/paymentCalendar/dates";
import { riskLabel } from "./messages";
const props = defineProps({
	snapshot: { type: Object, required: true },
	day: { type: Object, default: null },
	selected: { type: String, required: true },
});
const money = (value) =>
	exactMoney(value, props.snapshot.currency, props.snapshot.projection.precision);
const riskClass = computed(() =>
	["red", "orange", "green"].includes(props.day?.risk) ? `risk-text-${props.day.risk}` : ""
);
const cards = computed(() => [
	{
		key: "cash",
		label: "Book cash now",
		value: money(props.snapshot.projection.book_cash),
		note: `Selected bank & cash accounts · ${dateLabel(props.snapshot.scope.anchor)}`,
	},
	{
		key: "payable",
		label: "Supplier dues",
		value: money(props.day?.payable_remaining),
		note: `Scheduled for ${dateLabel(props.selected)}`,
	},
	{
		key: "receivable",
		label: "Expected collections",
		value: money(props.day?.receivable_remaining),
		note: "Customer dues on selected date · not cash received",
	},
	{
		key: "closing",
		label: "Projected closing cash",
		value: money(props.day?.projected_closing),
		note:
			props.selected < props.snapshot.scope.anchor
				? `Forecast unavailable before ${dateLabel(props.snapshot.scope.anchor)}`
				: riskLabel(props.day?.risk),
	},
]);
</script>
