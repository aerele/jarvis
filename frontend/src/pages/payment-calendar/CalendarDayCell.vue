<template>
	<div
		class="day"
		:class="{ selected, today: view.today, adjacent: view.adjacent }"
		:data-cell-date="view.date"
	>
		<button
			class="day-header day-trigger"
			:class="{ 'trough-target': trough }"
			:data-date="view.date"
			:aria-pressed="selected"
			:aria-label="view.label"
			:aria-current="view.today ? 'date' : undefined"
			:tabindex="tabbable ? 0 : -1"
			:disabled="!view.selectable"
			@click="$emit('select', view.date)"
		>
			<span class="date-number">{{ Number(view.date.slice(-2)) }}</span
			><span class="cell-markers"
				><span v-if="view.star" class="hotspot" :title="view.star" aria-hidden="true"
					>★</span
				><span v-if="showStatus" class="status" :class="view.status.kind"
					><i v-if="view.status.kind !== 'neutral'"></i>{{ view.status.text }}</span
				></span
			>
		</button>
		<template v-if="view.selectable">
			<div v-if="view.activity" class="day-amounts">
				<button
					v-for="side in view.sides"
					:key="side.side"
					type="button"
					class="amount-line"
					:class="{ incoming: side.side === 'Receivable' }"
					:aria-label="`View ${side.side.toLowerCase()} invoices for ${view.short}: ${
						side.accessible
					}`"
					@click="$emit('select', view.date, side.side)"
				>
					<span class="label">{{ side.label }}</span
					><b>{{ side.short }}</b>
				</button>
			</div>
			<span v-if="view.net" class="day-net" :class="view.net.sign" :title="view.net.title">{{
				view.net.text
			}}</span>
			<span v-if="view.dash" class="quiet-day" :aria-label="view.dash">—</span>
		</template>
	</div>
</template>
<script setup>
import { computed } from "vue";
import "./tokens.css";
import "./CalendarDayCell.css";

const props = defineProps({
	view: { type: Object, required: true },
	selected: { type: Boolean, default: false },
	tabbable: { type: Boolean, default: false },
	// The date a "View {date}" jump landed on (outlined until its first blur).
	trough: { type: Boolean, default: false },
});
defineEmits(["select"]);
// "No dues" shows no marker; a neutral state ("Loading…", "Load month") shows text only.
const showStatus = computed(() => props.view.status && props.view.status.kind !== "none");
</script>
