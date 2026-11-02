<template>
	<section
		class="pcal-panel upcoming-panel"
		aria-labelledby="upcoming-heading"
		:aria-busy="loading"
	>
		<div class="pcal-panel-heading">
			<div>
				<h2 id="upcoming-heading">Upcoming payments</h2>
				<p>
					{{ dateLabel(addDays(selected, 1)) }} – {{ dateLabel(addDays(selected, 30)) }}
				</p>
			</div>
		</div>
		<p v-if="snapshot.unavailable?.Payable" class="pcal-empty">
			Upcoming payments unavailable for this scope.
		</p>
		<p v-else-if="loading" class="pcal-empty" role="status">Loading upcoming payments…</p>
		<p v-else-if="!rows.length" class="pcal-empty">
			No upcoming payments in the next 30 days.
		</p>
		<ul v-else class="upcoming-list">
			<li v-for="row in visible" :key="row.date" :data-upcoming-date="row.date">
				<component
					:is="isSelectable(row.date, snapshot.scope) ? 'button' : 'div'"
					class="upcoming-row"
					:class="{ informational: !isSelectable(row.date, snapshot.scope) }"
					@click="select(row.date)"
				>
					<span class="upcoming-date"
						><strong>{{ dateLabel(row.date) }}</strong
						><small
							>{{ row.payees }} {{ row.payees === 1 ? "payee" : "payees" }} ·
							{{ row.rows }} {{ row.rows === 1 ? "row" : "rows" }}</small
						><small v-if="row.overdue" class="risk-text-red">Overdue</small
						><small v-if="!isSelectable(row.date, snapshot.scope)"
							>Beyond calendar range</small
						></span
					>
					<span class="upcoming-amount">{{ money(row.remaining) }}</span>
				</component>
			</li>
		</ul>
		<button
			v-if="rows.length > 5"
			class="pcal-text-button upcoming-more"
			@click="expanded = !expanded"
		>
			{{ expanded ? "Show fewer dates" : `Show all ${rows.length} dates` }}
		</button>
		<p class="pcal-footnote">
			After the selected date, through day 30 inclusive. Select a date to see its details.
		</p>
	</section>
</template>
<script setup>
import { computed, ref, watch } from "vue";
import { addDays, dateLabel, isSelectable } from "@/lib/paymentCalendar/dates";
import { exactMoney } from "@/lib/paymentCalendar/format";
const props = defineProps({
	snapshot: { type: Object, required: true },
	selected: { type: String, required: true },
	rows: { type: Array, default: () => [] },
	loading: Boolean,
});
const emit = defineEmits(["select"]);
const expanded = ref(false),
	visible = computed(() => (expanded.value ? props.rows : props.rows.slice(0, 5)));
watch(
	() => props.selected,
	() => {
		expanded.value = false;
	}
);
const money = (value) =>
	exactMoney(value, props.snapshot.currency, props.snapshot.projection.precision);
function select(date) {
	if (isSelectable(date, props.snapshot.scope)) emit("select", date);
}
</script>
