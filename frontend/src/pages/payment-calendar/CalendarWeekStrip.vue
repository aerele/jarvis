<template>
	<div
		class="week-strip"
		aria-label="Selected week"
		@touchstart.passive="onTouchStart"
		@touchend="onTouchEnd"
	>
		<button
			class="icon-btn"
			aria-label="Previous week"
			:disabled="!isSelectable(step(-7), scope)"
			@click="$emit('select', step(-7))"
		>
			‹
		</button>
		<div class="week-days">
			<button
				v-for="date in dates"
				:key="date"
				class="week-day"
				:class="{ selected: date === selected, 'trough-target': date === troughDate }"
				:data-week-date="date"
				:aria-pressed="date === selected"
				:disabled="!isSelectable(date, scope)"
				@click="$emit('select', date)"
			>
				<span>{{ weekdayShort(date) }}</span
				><strong>{{ Number(date.slice(-2)) }}</strong
				><i :class="{ activity: hasActivity(days[date]) }"></i>
			</button>
		</div>
		<button
			class="icon-btn"
			aria-label="Next week"
			:disabled="!isSelectable(step(7), scope)"
			@click="$emit('select', step(7))"
		>
			›
		</button>
	</div>
</template>
<script setup>
import { computed } from "vue";
import { addDays, isSelectable, weekdayShort, weekStrip } from "@/lib/paymentCalendar/dates";
import { hasActivity } from "./calendarDay";
import "./tokens.css";
import "./CalendarWeekStrip.css";

// A horizontal move of more than this many pixels is a swipe; less is a scroll or a tap.
const SWIPE_PX = 50;

const props = defineProps({
	selected: { type: String, required: true },
	days: { type: Object, default: () => ({}) },
	scope: { type: Object, required: true },
	troughDate: { type: String, default: "" },
});
const emit = defineEmits(["select"]);
const dates = computed(() => weekStrip(props.selected));
const step = (offset) => addDays(props.selected, offset);

let touchStartX = null;
function onTouchStart(event) {
	touchStartX = event.changedTouches[0].clientX;
}
function onTouchEnd(event) {
	if (touchStartX === null) return;
	const delta = event.changedTouches[0].clientX - touchStartX;
	touchStartX = null;
	if (Math.abs(delta) > SWIPE_PX) {
		const next = step(delta < 0 ? 7 : -7);
		if (isSelectable(next, props.scope)) emit("select", next);
	}
}
</script>
