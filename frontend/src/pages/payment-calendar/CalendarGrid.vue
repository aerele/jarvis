<template>
	<section
		ref="root"
		class="pcal-cal"
		aria-label="Payment Calendar"
		:aria-busy="loading"
		@focusin="onFocusIn"
		@focusout="onFocusOut"
		@keydown="onKeydown"
	>
		<div class="calendar-toolbar">
			<div class="month-nav">
				<button
					id="previous-month"
					class="icon-btn"
					aria-label="Previous month"
					:disabled="atStart"
					:title="atStart ? OUT_OF_RANGE : undefined"
					@click="browse(-1)"
				>
					‹
				</button>
				<h2 id="month-heading">{{ monthLabel(month) }}</h2>
				<button
					id="next-month"
					class="icon-btn"
					aria-label="Next month"
					:disabled="atEnd"
					:title="atEnd ? OUT_OF_RANGE : undefined"
					@click="browse(1)"
				>
					›
				</button>
				<button id="today-button" class="btn" @click="$emit('today')">Today</button>
			</div>
			<div class="view-controls">
				<span class="currency">{{ snapshot.currency }}</span>
				<div class="segmented" role="group" aria-label="Calendar display">
					<button
						v-for="choice in choices"
						:id="`${choice}-button`"
						:key="choice"
						:class="{ active: mode === choice }"
						:aria-pressed="mode === choice"
						@click="choose(choice)"
					>
						{{ choice[0].toUpperCase() + choice.slice(1) }}
					</button>
				</div>
			</div>
		</div>
		<p v-if="stateText || loadError" class="pcal-cal-state" role="status">
			<template v-if="loadError"
				>Couldn’t load this month.
				<template v-if="issue">{{ explain(issue.code) }} </template
				><button v-if="issue?.code !== 'forbidden'" @click="$emit('retry')">
					Retry
				</button></template
			>
			<template v-else>{{ stateText }}</template>
		</p>
		<CalendarWeekStrip
			v-if="narrow && mode !== 'month'"
			:selected="selected"
			:days="snapshot.projection.days"
			:scope="snapshot.scope"
			:trough-date="troughDate"
			@select="onSelect"
		/>
		<p v-if="mode === 'week'" class="week-help">
			Select a day. Swipe the week to move forward or back.
		</p>
		<div v-else-if="mode === 'month'" class="grid" role="grid" :aria-label="monthLabel(month)">
			<div class="weekdays" role="row">
				<span v-for="name in WEEKDAYS" :key="name" role="columnheader">{{ name }}</span>
			</div>
			<div v-for="(week, index) in weeks" :key="index" class="week" role="row">
				<div v-for="date in week" :key="date" role="gridcell">
					<CalendarDayCell
						:view="views.get(date)"
						:selected="date === selected"
						:tabbable="date === focusDate"
						:trough="date === troughDate"
						@select="onSelect"
					/>
				</div>
			</div>
		</div>
		<CalendarListView
			v-else
			ref="listView"
			:views="listDates.map((date) => views.get(date))"
			:selected="selected"
			:focus-date="focusDate"
			@select="onSelect"
		/>
		<p v-if="showNoData" class="calendar-unavailable">
			No payment or forecast data is available for {{ monthLabel(month) }}.
		</p>
		<footer class="calendar-footer">
			<div class="legend" role="group" aria-label="Supplier payment status">
				<span class="legend-title">Supplier status</span>
				<span><i class="green"></i>Settled</span>
				<span><i class="amber"></i>Partial</span>
				<span><i class="red"></i>Unpaid</span>
				<span><i></i>No dues</span>
				<span class="hotspot-legend"
					><b aria-hidden="true">★</b> Single invoice &gt;50%</span
				>
			</div>
		</footer>
	</section>
</template>
<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { addDays, isSelectable, monthGrid, monthLabel } from "@/lib/paymentCalendar/dates";
import CalendarDayCell from "./CalendarDayCell.vue";
import CalendarListView from "./CalendarListView.vue";
import CalendarWeekStrip from "./CalendarWeekStrip.vue";
import { DayViews } from "./calendarDay";
import { explain } from "./messages";
import "./tokens.css";
import "./CalendarGrid.css";

const OUT_OF_RANGE = "Outside the calendar range";
// Sunday first, like monthGrid() in lib/paymentCalendar/dates.js: keep in sync with it.
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
// The phone layout starts at this container width (the template's 540 px breakpoint).
const PHONE_MAX_WIDTH = 540;

const props = defineProps({
	snapshot: { type: Object, required: true },
	selected: { type: String, required: true },
	month: { type: String, required: true },
	mode: { type: String, default: undefined },
	loading: { type: Boolean, default: false },
	waiting: { type: Boolean, default: false },
	loadError: { type: Boolean, default: false },
	// The core's refreshIssue ({ code }): explains a failed load; forbidden has no Retry.
	issue: { type: Object, default: null },
});
const emit = defineEmits(["select", "browse", "today", "retry", "update:mode"]);

const root = ref(null);
const listView = ref(null);
const scope = computed(() => props.snapshot.scope);

// Layout width: week mode follows the container, like the CSS.
const width = ref(0);
const narrow = computed(() => width.value > 0 && width.value <= PHONE_MAX_WIDTH);
let observer;
function watchLayoutWidth() {
	const layout = root.value?.closest(".pcal-layout");
	if (!layout) return;
	width.value = layout.clientWidth;
	if (typeof ResizeObserver === "undefined") return;
	observer = new ResizeObserver((entries) => (width.value = entries[0].contentRect.width));
	observer.observe(layout);
}
onMounted(() => {
	watchLayoutWidth();
	listView.value?.revealSelection();
});
onBeforeUnmount(() => observer?.disconnect());

const chosen = ref(props.mode ?? null);
watch(
	() => props.mode,
	(value) => (chosen.value = value ?? null)
);
const choices = computed(() => (narrow.value ? ["week", "month", "list"] : ["month", "list"]));
const mode = computed(() => {
	const wanted = chosen.value || (narrow.value ? "week" : "month");
	return choices.value.includes(wanted) ? wanted : "month";
});
function choose(choice) {
	clearTrough();
	chosen.value = choice;
	emit("update:mode", choice);
}

const atStart = computed(() => props.month <= scope.value.selectable_start.slice(0, 7));
const atEnd = computed(() => props.month >= scope.value.selectable_end.slice(0, 7));
const stateText = computed(() =>
	props.waiting ? "Waiting to load this month…" : props.loading ? "Loading values…" : ""
);

const grid = computed(() => monthGrid(props.month));
const weeks = computed(() =>
	Array.from({ length: grid.value.length / 7 }, (_, i) => grid.value.slice(i * 7, i * 7 + 7))
);
const listDates = computed(() => grid.value.filter((date) => date.startsWith(props.month)));
const visibleDates = computed(() =>
	mode.value === "month" ? grid.value : mode.value === "list" ? listDates.value : []
);
const views = computed(() => {
	const model = new DayViews(props);
	return new Map(grid.value.map((date) => [date, model.view(date)]));
});
const showNoData = computed(
	() =>
		!props.loading &&
		!props.waiting &&
		!props.loadError &&
		!Object.keys(props.snapshot.projection.days).some((date) => date.startsWith(props.month))
);

const focusDate = ref(props.selected);
watch([() => props.month, () => props.selected, mode], () => {
	focusDate.value = visibleDates.value.includes(props.selected)
		? props.selected
		: visibleDates.value.find((date) => isSelectable(date, scope.value));
});
const onFocusIn = (event) => {
	const date = event.target.closest?.("[data-date]")?.dataset.date;
	if (date) focusDate.value = date;
};

// Trough jump: the date a "View {date}" jump landed on keeps its outline until its first
// blur. One reactive date means one target; a jump to a date not drawn yet waits (pending).
const troughDate = ref("");
let pendingTrough = "";
const clearTrough = () => {
	troughDate.value = "";
	pendingTrough = "";
};
async function revealTrough(date) {
	troughDate.value = "";
	pendingTrough = date;
	chosen.value = null; // default view: week on a phone, else month (leaves the list)
	await nextTick();
	await landTrough();
}
async function landTrough() {
	const date = pendingTrough;
	const target = date && root.value?.querySelector(troughSelector(date));
	if (!target) return; // the month is not drawn yet; retried when the view changes
	pendingTrough = "";
	troughDate.value = date;
	await nextTick();
	target.focus({ preventScroll: true });
	(narrow.value ? root.value : target)?.scrollIntoView?.({
		block: narrow.value ? "start" : "center",
	});
}
const troughSelector = (date) =>
	mode.value === "week" ? `[data-week-date="${date}"]` : `[data-date="${date}"]`;
const onFocusOut = (event) => {
	if (event.target.classList?.contains("trough-target")) troughDate.value = "";
};
watch([() => props.month, mode], () => (troughDate.value = ""));
// Choosing any other date ends a jump, landed or still waiting for its month.
watch(
	() => props.selected,
	(date) => {
		if (date !== troughDate.value) troughDate.value = "";
		if (date !== pendingTrough) pendingTrough = "";
	}
);
watch([() => props.month, () => props.selected, mode], landTrough, { flush: "post" });

function browse(offset) {
	clearTrough();
	emit("browse", offset);
}

// A date button selects the date and keeps the side; an amount line also picks the side.
function onSelect(date, side) {
	clearTrough();
	if (side) emit("select", date, side);
	else emit("select", date);
}

// Arrows move focus only. They stay inside the drawn dates and the selectable range.
function onKeydown(event) {
	const date = event.target.closest?.("[data-date]")?.dataset.date;
	if (!date) return;
	const step = arrowStep(event.key);
	if (!step) return;
	event.preventDefault();
	const next = addDays(date, step);
	if (visibleDates.value.includes(next) && isSelectable(next, scope.value)) moveFocus(next);
}
function arrowStep(key) {
	const down = mode.value === "list" ? 1 : 7;
	return { ArrowRight: 1, ArrowLeft: -1, ArrowDown: down, ArrowUp: -down }[key];
}
function moveFocus(date) {
	clearTrough();
	focusDate.value = date;
	root.value.querySelector(`[data-date="${date}"]`)?.focus();
}

watch([mode, () => props.month, () => props.selected], () => listView.value?.revealSelection(), {
	flush: "post",
});
defineExpose({ revealTrough });
</script>
