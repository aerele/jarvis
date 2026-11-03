<template>
	<section class="pcal-panel" aria-label="Payment Calendar" :aria-busy="loading">
		<div class="pcal-panel-heading calendar-heading">
			<div>
				<h2>{{ monthLabel(month) }}</h2>
				<p role="status">
					<template v-if="loadError"
						>Couldn’t load this month.
						<button class="pcal-text-button" @click="$emit('retry')">
							Retry
						</button></template
					>
					<template v-else-if="waiting">Waiting to load this month…</template>
					<template v-else-if="loading">Loading values…</template>
					<template v-else>Remaining supplier dues by due date</template>
				</p>
			</div>
			<div class="pcal-actions">
				<button
					class="pcal-icon"
					aria-label="Previous month"
					:disabled="month <= scope.selectable_start.slice(0, 7)"
					@click="$emit('browse', -1)"
				>
					‹
				</button>
				<button class="pcal-button" @click="$emit('today')">Today</button>
				<button
					class="pcal-icon"
					aria-label="Next month"
					:disabled="month >= scope.selectable_end.slice(0, 7)"
					@click="$emit('browse', 1)"
				>
					›
				</button>
			</div>
		</div>
		<div
			v-if="mode === 'month'"
			role="grid"
			:aria-label="monthLabel(month)"
			class="calendar-grid"
		>
			<div role="row" class="calendar-weekdays">
				<span v-for="name in weekdays" :key="name" role="columnheader">{{ name }}</span>
			</div>
			<div v-for="(week, index) in weeks" :key="index" role="row" class="calendar-week">
				<div
					v-for="date in week"
					:key="date"
					role="gridcell"
					:aria-selected="date === selected"
				>
					<button
						:ref="(el) => setButton(date, el)"
						:data-date="date"
						:disabled="!isSelectable(date, scope)"
						:tabindex="date === focusDate ? 0 : -1"
						:aria-label="label(date)"
						:aria-current="date === scope.anchor ? 'date' : undefined"
						class="calendar-cell"
						:class="classes(date)"
						@focus="focusDate = date"
						@click="$emit('select', date)"
						@keydown="onKey($event, date)"
					>
						<span class="calendar-date" :class="{ today: date === scope.anchor }">{{
							Number(date.slice(-2))
						}}</span>
						<template v-if="isSelectable(date, scope)"
							><span
								class="calendar-amount"
								:title="money(day(date)?.payable_remaining)"
								><span class="calendar-amount-full">{{
									compact(day(date)?.payable_remaining)
								}}</span
								><span class="calendar-amount-narrow">{{
									compact(day(date)?.payable_remaining, 6)
								}}</span></span
							><span class="calendar-settlement">{{
								settlement(date)
							}}</span></template
						>
					</button>
				</div>
			</div>
		</div>
		<div v-else ref="listElement" class="calendar-list">
			<button
				v-for="date in listDates"
				:key="date"
				:data-date="date"
				:aria-label="label(date)"
				:aria-current="date === scope.anchor ? 'date' : undefined"
				:tabindex="date === focusDate ? 0 : -1"
				:ref="(el) => setButton(date, el)"
				class="calendar-list-row"
				:class="classes(date)"
				@focus="focusDate = date"
				@keydown="onKey($event, date)"
				@click="$emit('select', date)"
			>
				<span
					>{{ dateLabel(date)
					}}<small
						>{{ date === scope.anchor ? "Today · " : "" }}{{ settlement(date) }}</small
					></span
				><span class="list-amount">{{ money(day(date)?.payable_remaining) }}</span>
			</button>
		</div>
		<div class="calendar-legend">
			<span><i class="settlement-dot green" aria-hidden="true"></i>Paid / Settled</span
			><span
				><i class="settlement-dot orange" aria-hidden="true"></i>Partially paid /
				settled</span
			><span><i class="settlement-dot red" aria-hidden="true"></i>Unpaid</span
			><span
				><i class="settlement-dot neutral" aria-hidden="true"></i>No dues /
				Unavailable</span
			>
		</div>
		<p class="pcal-footnote">Payment status is as of this snapshot.</p>
	</section>
</template>
<script setup>
import { computed, nextTick, onMounted, ref, watch } from "vue";
import {
	addDays,
	addMonths,
	dateLabel,
	isSelectable,
	monthGrid,
	monthLabel,
} from "@/lib/paymentCalendar/dates";
import { compactMoney, exactMoney } from "@/lib/paymentCalendar/format";
const props = defineProps({
	snapshot: { type: Object, required: true },
	selected: { type: String, required: true },
	month: { type: String, required: true },
	mode: { type: String, default: "month" },
	loading: { type: Boolean, default: false },
	waiting: { type: Boolean, default: false },
	loadError: { type: Boolean, default: false },
});
const emit = defineEmits(["select", "browse", "today", "retry"]);
const scope = computed(() => props.snapshot.scope),
	grid = computed(() => monthGrid(props.month));
const weeks = computed(() =>
	Array.from({ length: 6 }, (_, i) => grid.value.slice(i * 7, i * 7 + 7))
);
const listDates = computed(() =>
	grid.value.filter((date) => date.startsWith(props.month) && isSelectable(date, scope.value))
);
const weekdays = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const focusDate = ref(props.selected),
	buttons = new Map();
const listElement = ref(null);
function revealSelection() {
	const element = listElement.value,
		selected = buttons.get(focusDate.value);
	if (props.mode === "list" && element && selected)
		element.scrollTop = Math.max(
			0,
			selected.offsetTop - (element.clientHeight - selected.clientHeight) / 2
		);
}
onMounted(revealSelection);
watch([() => props.mode, () => props.month, () => props.selected], revealSelection, {
	flush: "post",
});
const visibleDates = computed(() => (props.mode === "month" ? grid.value : listDates.value));
watch([() => props.month, () => props.selected, () => props.mode], () => {
	focusDate.value = visibleDates.value.includes(props.selected)
		? props.selected
		: visibleDates.value.find((date) => isSelectable(date, scope.value));
});
function setButton(date, element) {
	if (element) buttons.set(date, element);
	else buttons.delete(date);
}
const day = (date) => props.snapshot.projection.days[date];
const money = (value) =>
	exactMoney(value, props.snapshot.currency, props.snapshot.projection.precision);
const compact = (value, maxLength = 14) =>
	compactMoney(value, props.snapshot.currency, props.snapshot.projection.precision, maxLength);
const settlementColours = {
	Paid: "green",
	Settled: "green",
	"Partially paid": "orange",
	"Partially settled": "orange",
	Unpaid: "red",
	"No dues": null,
};
function settlement(date) {
	if ((props.loading || props.loadError) && !day(date) && date.startsWith(props.month + "-"))
		return props.loadError ? "Not loaded" : "Loading…";
	if (
		scope.value.month &&
		!date.startsWith(scope.value.month + "-") &&
		isSelectable(date, scope.value)
	)
		return "Load month";
	const status = day(date)?.settlement;
	return props.snapshot.projection.payable_settlement_complete === true &&
		props.snapshot.projection.reconciliation?.Payable != null &&
		!props.snapshot.unavailable?.Payable &&
		isSelectable(date, scope.value) &&
		typeof status === "string" &&
		Object.hasOwn(settlementColours, status)
		? status
		: "Unavailable";
}
function label(date) {
	return `${dateLabel(date, true)}, ${money(day(date)?.payable_remaining)}, ${settlement(date)}`;
}
function classes(date) {
	const colour = settlementColours[settlement(date)];
	return {
		selected: date === props.selected,
		adjacent: !date.startsWith(props.month),
		[`settlement-${colour}`]: Boolean(colour),
	};
}
async function onKey(event, date) {
	if (["Enter", " "].includes(event.key)) {
		event.preventDefault();
		emit("select", date);
		return;
	}
	const offsets = {
		ArrowRight: 1,
		ArrowLeft: -1,
		ArrowDown: props.mode === "list" ? 1 : 7,
		ArrowUp: props.mode === "list" ? -1 : -7,
	};
	let next;
	if (event.key in offsets) next = addDays(date, offsets[event.key]);
	else if (event.key === "Home" || event.key === "End") {
		const weekday = new Date(date + "T12:00:00Z").getUTCDay();
		next = addDays(date, event.key === "Home" ? -weekday : 6 - weekday);
	} else if (["PageUp", "PageDown"].includes(event.key))
		next = addMonths(date.slice(0, 7), event.key === "PageUp" ? -1 : 1) + "-01";
	else return;
	event.preventDefault();
	if (!isSelectable(next, scope.value)) return;
	if (!visibleDates.value.includes(next)) {
		emit("browse", next < props.month + "-01" ? -1 : 1);
		await nextTick();
	}
	focusDate.value = next;
	buttons.get(next)?.focus();
}
</script>
