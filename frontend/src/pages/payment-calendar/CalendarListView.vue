<template>
	<div ref="list" class="list-view">
		<template v-for="view in views" :key="view.date">
			<div v-if="startsWeek(view.date)" class="list-week-label">
				{{ weekLabel(view.date) }}
			</div>
			<div
				class="date-row"
				:class="{ selected: view.date === selected }"
				:data-list-date="view.date"
			>
				<button
					class="date-row-date"
					:data-date="view.date"
					:aria-current="view.today ? 'date' : undefined"
					:tabindex="view.date === focusDate ? 0 : -1"
					:disabled="!view.selectable"
					@click="$emit('select', view.date)"
				>
					<b>{{ view.short }}</b
					><small>{{ view.line }}</small>
				</button>
				<button
					v-for="side in view.sides"
					:key="side.side"
					type="button"
					class="amount-line list-amount"
					:class="{ incoming: side.side === 'Receivable' }"
					:disabled="!view.entry || !view.selectable"
					:aria-label="`View ${side.side.toLowerCase()} invoices for ${view.short}: ${
						side.accessible
					}`"
					@click="$emit('select', view.date, side.side)"
				>
					<span class="label">{{ side.label }}</span
					><b>{{ side.short }}</b>
				</button>
				<span class="list-net" :class="{ negative: view.listNet.negative }">{{
					view.listNet.text
				}}</span>
			</div>
		</template>
	</div>
</template>
<script setup>
import { ref } from "vue";
import { weekLabel } from "@/lib/paymentCalendar/dates";
import "./tokens.css";
import "./CalendarListView.css";

defineProps({
	views: { type: Array, required: true },
	selected: { type: String, required: true },
	focusDate: { type: String, default: "" },
});
defineEmits(["select"]);
const list = ref(null);
// A week label opens the list and every Monday.
const startsWeek = (date) =>
	date.endsWith("-01") || new Date(date + "T12:00:00Z").getUTCDay() === 1;

// Centre the selected row inside the list's own scroll area.
function revealSelection() {
	const element = list.value,
		row = element?.querySelector(".date-row.selected");
	if (element && row)
		element.scrollTop = Math.max(
			0,
			row.offsetTop - (element.clientHeight - row.clientHeight) / 2
		);
}
defineExpose({ revealSelection });
</script>
