<template>
	<section class="pcal-panel" aria-label="Payment Calendar">
		<div class="pcal-panel-heading calendar-heading">
			<div>
				<h2>{{ monthLabel(month) }}</h2>
				<p>Remaining supplier dues by due date</p>
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
						<span class="calendar-date"
							>{{ Number(date.slice(-2))
							}}<span
								v-if="date === scope.anchor"
								class="today-dot"
								aria-hidden="true"
							></span
						></span>
						<template v-if="isSelectable(date, scope)"
							><span
								class="calendar-amount"
								:title="money(day(date)?.payable_remaining)"
								>{{ compact(day(date)?.payable_remaining) }}</span
							><span class="calendar-settlement">{{
								day(date)?.settlement || "Unavailable"
							}}</span
							><span v-if="day(date)?.payable_terms" class="calendar-count"
								>{{ day(date).payable_terms }} invoice
								{{ day(date).payable_terms === 1 ? "row" : "rows" }}</span
							><span
								v-if="riskMarker(day(date)?.risk)"
								class="calendar-risk"
								aria-hidden="true"
								>{{ riskMarker(day(date)?.risk) }}</span
							></template
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
						>{{ date === scope.anchor ? "Today · " : ""
						}}{{ day(date)?.settlement || "Unavailable" }}</small
					></span
				><span class="list-amount"
					>{{ money(day(date)?.payable_remaining)
					}}<small>{{ riskLabel(day(date)?.risk) }}</small></span
				>
			</button>
		</div>
		<div class="calendar-legend">
			<span><i class="risk-dot red" aria-hidden="true">↓</i>Below zero</span
			><span><i class="risk-dot orange" aria-hidden="true">!</i>Below minimum</span
			><span><i class="risk-dot green" aria-hidden="true">✓</i>At / above minimum</span
			><span><i class="risk-dot unknown"></i>Unavailable / historical</span>
		</div>
		<p class="pcal-footnote">
			Colours describe projected closing cash. Paid / Settled describes ERP accounting
			settlement, not bank clearance.
		</p>
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
import { riskLabel } from "./messages";
const props = defineProps({
	snapshot: { type: Object, required: true },
	selected: { type: String, required: true },
	month: { type: String, required: true },
	mode: { type: String, default: "month" },
});
const emit = defineEmits(["select", "browse", "today"]);
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
const compact = (value) =>
	compactMoney(value, props.snapshot.currency, props.snapshot.projection.precision);
function label(date) {
	return `${dateLabel(date, true)}, ${money(day(date)?.payable_remaining)}, ${
		day(date)?.settlement || "Unavailable"
	}, ${riskLabel(day(date)?.risk)}`;
}
function classes(date) {
	const risk = day(date)?.risk;
	return {
		selected: date === props.selected,
		adjacent: !date.startsWith(props.month),
		[`risk-bg-${risk}`]:
			isSelectable(date, scope.value) && ["red", "orange", "green"].includes(risk),
	};
}
const riskMarker = (risk) => ({ red: "↓ Short", orange: "! Low", green: "✓ OK" }[risk] || "");
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
