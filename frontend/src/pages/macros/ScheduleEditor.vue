<template>
	<!-- The schedule's draft, as the owner edits it: Repeat, then what that repeat
	     needs (a date, days, times, a cron), then what it means in words and the next
	     runs. A fieldset so one `disabled` reaches every control, the tab buttons too. -->
	<fieldset
		class="m-0 flex min-w-0 flex-col gap-5 border-0 p-0"
		:disabled="disabled"
		data-testid="schedule-editor"
	>
		<div>
			<span :id="ids.repeat" class="mb-1.5 block text-xs text-ink-gray-5">Repeat</span>
			<div class="flex flex-wrap" :aria-labelledby="ids.repeat" role="group">
				<TabButtons
					data-testid="schedule-repeat"
					:buttons="REPEAT_OPTIONS"
					:modelValue="model.frequency"
					@update:modelValue="setFrequency"
				/>
			</div>
		</div>

		<!-- A one-time schedule runs on one date, then switches itself off. -->
		<div v-if="model.frequency === 'once'" ref="dateField" class="w-56">
			<label :for="ids.date" class="mb-1.5 block text-xs text-ink-gray-5">Date</label>
			<!-- The picker's own input takes no id: this one does, so the label above
			     names it, and it shows the date as people read it. -->
			<DatePicker
				:modelValue="scheduleDate"
				format="ddd, D MMM YYYY"
				placeholder="Pick a date"
				:disabled="disabled"
				@update:modelValue="(v) => emit('update:scheduleDate', v || '')"
			>
				<template #target="{ togglePopover, isOpen, displayLabel }">
					<TextInput
						:id="ids.date"
						class="w-full cursor-pointer"
						readonly
						aria-haspopup="dialog"
						:aria-expanded="isOpen ? 'true' : 'false'"
						:modelValue="displayLabel"
						placeholder="Pick a date"
						:disabled="disabled"
						@click="togglePopover()"
						@keydown.enter.prevent="togglePopover()"
						@keydown.space.prevent="togglePopover()"
					/>
				</template>
			</DatePicker>
			<ErrorMessage :message="dateError" class="mt-2" />
		</div>

		<div v-if="model.frequency === 'hourly'">
			<span :id="ids.every" class="mb-1.5 block text-xs text-ink-gray-5">Every</span>
			<div
				class="flex flex-wrap items-center gap-2 text-base text-ink-gray-8"
				role="group"
				:aria-labelledby="ids.every"
			>
				<select
					class="jv-sched-select w-20"
					aria-label="Every how many hours"
					:value="model.everyN"
					@change="(e) => update({ everyN: e.target.value })"
				>
					<option v-for="n in EVERY_N_HOURS" :key="n" :value="n">{{ n }}</option>
				</select>
				<span>{{ model.everyN === "1" ? "hour" : "hours" }}, at</span>
				<select
					class="jv-sched-select w-20"
					aria-label="Minutes past the hour"
					:value="model.hourlyMin"
					@change="(e) => update({ hourlyMin: e.target.value })"
				>
					<option
						v-for="o in minuteOptions(model.hourlyMin, ':')"
						:key="o.value"
						:value="o.value"
					>
						{{ o.label }}
					</option>
				</select>
				<span>past the hour</span>
			</div>
		</div>

		<div v-if="model.frequency === 'weekly'">
			<span :id="ids.days" class="mb-1.5 block text-xs text-ink-gray-5">On</span>
			<div class="flex flex-wrap gap-1.5" role="group" :aria-labelledby="ids.days">
				<button
					v-for="d in WEEK"
					:key="d"
					type="button"
					class="jv-sched-chip"
					:class="{ on: model.days.includes(d) }"
					:aria-pressed="model.days.includes(d) ? 'true' : 'false'"
					:aria-label="DAY_LONG[d]"
					:data-day="d"
					@click="toggleIn('days', d)"
				>
					{{ DAY_SHORT[d] }}
				</button>
			</div>
			<div class="mt-2 flex gap-3.5 text-sm">
				<button
					type="button"
					class="jv-sched-quick"
					@click="update({ days: [1, 2, 3, 4, 5] })"
				>
					Weekdays
				</button>
				<button type="button" class="jv-sched-quick" @click="update({ days: [6, 0] })">
					Weekend
				</button>
				<button type="button" class="jv-sched-quick" @click="update({ days: [...WEEK] })">
					Every day
				</button>
			</div>
		</div>

		<div v-if="model.frequency === 'monthly'">
			<span :id="ids.dom" class="mb-1.5 block text-xs text-ink-gray-5">
				On these days of the month
			</span>
			<div class="jv-sched-dom" role="group" :aria-labelledby="ids.dom">
				<button
					v-for="n in 31"
					:key="n"
					type="button"
					class="jv-sched-chip"
					:class="{ on: model.dom.includes(n) }"
					:aria-pressed="model.dom.includes(n) ? 'true' : 'false'"
					:aria-label="`Day ${n}`"
					:data-dom="n"
					@click="toggleIn('dom', n)"
				>
					{{ n }}
				</button>
			</div>
			<p class="mt-1.5 text-xs text-ink-gray-5">
				Months without the 29th, 30th or 31st skip that day.
			</p>
		</div>

		<div v-if="showsTimes">
			<span class="mb-1.5 block text-xs text-ink-gray-5">{{
				shownTimes.length > 1 ? "Times" : "Time"
			}}</span>
			<div class="flex flex-col gap-2">
				<div
					v-for="(t, i) in shownTimes"
					:key="i"
					class="flex items-center gap-2"
					role="group"
					:aria-label="`Time ${i + 1}`"
					data-testid="schedule-time"
				>
					<select
						class="jv-sched-select w-[4.5rem]"
						aria-label="Hour"
						:value="t.h"
						@change="(e) => setTime(i, { h: e.target.value })"
					>
						<option v-for="h in HOURS" :key="h" :value="h">{{ h }}</option>
					</select>
					<span class="font-semibold text-ink-gray-5" aria-hidden="true">:</span>
					<select
						class="jv-sched-select w-[4.5rem]"
						aria-label="Minute"
						:value="t.m"
						@change="(e) => setTime(i, { m: e.target.value })"
					>
						<option v-for="o in minuteOptions(t.m)" :key="o.value" :value="o.value">
							{{ o.label }}
						</option>
					</select>
					<TabButtons
						:buttons="AM_PM"
						:modelValue="t.ap"
						aria-label="AM or PM"
						@update:modelValue="(v) => v && setTime(i, { ap: v })"
					/>
					<Button
						v-if="shownTimes.length > 1"
						variant="ghost"
						icon="x"
						:aria-label="`Remove time ${i + 1}`"
						@click="removeTime(i)"
					/>
				</div>
				<Button
					v-if="canAddTime"
					class="self-start"
					variant="ghost"
					iconLeft="plus"
					label="Add another time"
					@click="addTime"
				/>
			</div>
		</div>

		<div v-if="model.frequency === 'custom'">
			<label :for="ids.cron" class="mb-1.5 block text-xs text-ink-gray-5"
				>Cron schedule</label
			>
			<textarea
				:id="ids.cron"
				class="jv-sched-cron"
				:rows="Math.min(Math.max(cronLineCount, 1), 6)"
				spellcheck="false"
				autocomplete="off"
				:value="model.cron"
				:aria-describedby="ids.legend"
				@input="(e) => update({ cron: e.target.value })"
			/>
			<div :id="ids.legend" class="jv-sched-legend">
				<span>minute</span><span>hour</span><span>day</span><span>month</span
				><span>weekday</span>
			</div>
			<p class="mt-1 text-xs text-ink-gray-5">One schedule per line.</p>
			<div class="mt-2.5 flex flex-wrap gap-1.5">
				<button
					v-for="ex in CRON_EXAMPLES"
					:key="ex.cron"
					type="button"
					class="jv-sched-example"
					@click="update({ cron: ex.cron })"
				>
					{{ ex.label }}
				</button>
			</div>
		</div>

		<!-- What the draft means: in words, the next runs, and what it costs. Live, so a
		     screen reader hears it settle as the controls change. -->
		<div class="jv-sched-summary" aria-live="polite" data-testid="schedule-preview">
			<p v-if="preview.error" class="flex items-start gap-2 text-sm text-ink-red-4">
				<span class="lucide-circle-alert mt-0.5 size-4 shrink-0" aria-hidden="true" />
				<span>{{ preview.error }}</span>
			</p>
			<template v-else>
				<p class="flex items-start gap-2 text-base font-medium text-ink-gray-9">
					<span class="lucide-clock mt-0.5 size-4 shrink-0" aria-hidden="true" />
					<span>{{ preview.summary }}</span>
				</p>
				<div>
					<span class="mb-1 block text-xs text-ink-gray-5">{{
						model.frequency === "once" ? "Runs on" : "Next runs"
					}}</span>
					<ul class="m-0 flex list-none flex-col gap-1 p-0 text-sm text-ink-gray-7">
						<li
							v-for="(n, i) in preview.next"
							:key="n"
							class="flex items-center gap-2.5"
							:class="{ 'font-medium text-ink-gray-9': i === 0 }"
						>
							<span
								class="size-1.5 rounded-full"
								:class="i === 0 ? 'bg-surface-gray-7' : 'bg-surface-gray-4'"
								aria-hidden="true"
							/>{{ n }}
						</li>
					</ul>
				</div>
				<p v-if="preview.tooClose" class="jv-sched-warn">
					{{ brand.agentName }} checks schedules every 5 minutes, so runs closer together
					than that are merged, and a run can start up to 5 minutes late.
				</p>
				<p
					v-if="preview.overBudget"
					class="jv-sched-warn"
					data-testid="schedule-over-budget"
				>
					That is more than the {{ budget }} scheduled steps a month this site allows.
					Runs after the limit is reached are skipped until next month.
				</p>
				<div class="jv-sched-meta">
					<span v-if="preview.cronText && model.frequency !== 'custom'">
						Saved as cron <code>{{ preview.cronText }}</code>
					</span>
					<span
						v-if="model.frequency !== 'once' && budget > 0"
						data-testid="schedule-budget"
					>
						About {{ preview.perMonth }} run{{ preview.perMonth === 1 ? "" : "s" }} in
						the next 30 days: {{ preview.stepsMonth }} of the {{ budget }} scheduled
						steps a month ({{ stepCount }} step{{ stepCount === 1 ? "" : "s" }} a run).
					</span>
					<span v-if="zone">Times are on the site's clock, {{ zone }}.</span>
				</div>
			</template>
		</div>
	</fieldset>
</template>

<script setup>
// The Schedule section's editor for a macro (MacroDetail). It owns no state: the
// draft comes in as `modelValue` (macroCron's shape) and every change goes out as a
// new object. What a draft saves as, and what it says, is macroCron's.
import { computed, ref } from "vue";
import { Button, DatePicker, ErrorMessage, TabButtons, TextInput, dayjs } from "frappe-ui";
import { brand } from "@/branding";
import { siteTimezone } from "@/utils/datetime";
import {
	DAY_LONG,
	DAY_SHORT,
	EVERY_N_HOURS,
	HOURS,
	MAX_TIMES,
	MINUTES,
	WEEK,
	cronLinesOf,
	presetCron,
	schedulePreview,
	wallClock,
} from "@/lib/macroCron";

const props = defineProps({
	modelValue: { type: Object, required: true },
	scheduleDate: { type: String, default: "" },
	dateError: { type: String, default: "" },
	disabled: { type: Boolean, default: false },
	// The macro's step count and the site's monthly step budget for scheduled runs
	// (0 when unknown: the estimate is left out).
	steps: { type: Number, default: 1 },
	budget: { type: Number, default: 0 },
});
const emit = defineEmits(["update:modelValue", "update:scheduleDate"]);

const REPEAT_OPTIONS = [
	{ label: "Once", value: "once" },
	{ label: "Hourly", value: "hourly" },
	{ label: "Daily", value: "daily" },
	{ label: "Weekly", value: "weekly" },
	{ label: "Monthly", value: "monthly" },
	{ label: "Custom", value: "custom" },
];
const AM_PM = [
	{ label: "AM", value: "AM" },
	{ label: "PM", value: "PM" },
];
const CRON_EXAMPLES = [
	{ label: "Weekdays at 9 am", cron: "0 9 * * 1-5" },
	{ label: "Every 30 min, 9 am to 6 pm", cron: "*/30 9-17 * * 1-5" },
	{ label: "Quarter start, 10 am", cron: "0 10 1 1,4,7,10 *" },
	{ label: "Fridays at 5:30 pm", cron: "30 17 * * 5" },
];

// Ids unique to this instance: two editors on a page must not share a label target.
const uid = Math.random().toString(36).slice(2, 8);
const ids = {
	repeat: `jv-sched-repeat-${uid}`,
	date: `macro-schedule-date`,
	every: `jv-sched-every-${uid}`,
	days: `jv-sched-days-${uid}`,
	dom: `jv-sched-dom-${uid}`,
	cron: `jv-sched-cron-${uid}`,
	legend: `jv-sched-legend-${uid}`,
};
const dateField = ref(null);
defineExpose({ dateField });

const model = computed(() => props.modelValue);
const zone = siteTimezone();
const stepCount = computed(() => Math.max(1, props.steps || 1));

function update(patch) {
	emit("update:modelValue", { ...model.value, ...patch });
}

function setFrequency(frequency) {
	if (!frequency || frequency === model.value.frequency) return;
	const patch = { frequency };
	// Custom starts from the schedule already chosen, so switching loses nothing.
	if (frequency === "custom" && !["once", "custom"].includes(model.value.frequency)) {
		try {
			patch.cron = presetCron(model.value).join("\n");
		} catch (e) {
			// a preset with no day picked: keep whatever cron there was
		}
	}
	update(patch);
}

function toggleIn(key, value) {
	const list = model.value[key];
	update({
		[key]: list.includes(value) ? list.filter((x) => x !== value) : [...list, value],
	});
}

// A one-time schedule runs at one time; the others keep all of theirs.
const shownTimes = computed(() =>
	model.value.frequency === "once" ? model.value.times.slice(0, 1) : model.value.times
);
const showsTimes = computed(() =>
	["once", "daily", "weekly", "monthly"].includes(model.value.frequency)
);
const canAddTime = computed(
	() => model.value.frequency !== "once" && model.value.times.length < MAX_TIMES
);

function setTime(i, patch) {
	update({ times: model.value.times.map((t, j) => (j === i ? { ...t, ...patch } : t)) });
}
function removeTime(i) {
	update({ times: model.value.times.filter((_, j) => j !== i) });
}
function addTime() {
	update({ times: [...model.value.times, { h: "6", m: "00", ap: "PM" }] });
}

// The five-minute steps, plus a minute saved before them (a macro set for 10:17
// keeps its 17 rather than showing nothing).
function minuteOptions(current, prefix = "") {
	const list = MINUTES.includes(current) || !current ? MINUTES : [...MINUTES, current].sort();
	return list.map((m) => ({ label: `${prefix}${m}`, value: m }));
}

const cronLineCount = computed(() => cronLinesOf(model.value.cron).length);

// The site's wall clock now, in macroCron's UTC parts.
function siteNow() {
	const d = zone ? dayjs().tz(zone) : dayjs();
	return wallClock(d.year(), d.month(), d.date(), d.hour(), d.minute());
}

const preview = computed(() =>
	schedulePreview(model.value, {
		now: siteNow(),
		scheduleDate: props.scheduleDate,
		steps: stepCount.value,
		budget: props.budget,
	})
);
</script>

<style scoped>
/* A native select in the inputs' look: small, fixed-width pickers whose menu is the
   platform's own (a wheel on a phone). */
.jv-sched-select {
	height: 1.75rem;
	padding: 0 1.5rem 0 0.625rem;
	border: 1px solid transparent;
	border-radius: 0.5rem;
	background-color: var(--surface-gray-2, #f3f3f3);
	background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%237c7c7c' stroke-width='2'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
	background-repeat: no-repeat;
	background-position: right 0.5rem center;
	color: var(--text-ink-gray-8, #383838);
	font-size: 14px;
	appearance: none;
	-webkit-appearance: none;
	cursor: pointer;
}
.jv-sched-select:hover {
	border-color: var(--outline-gray-2, #e2e2e2);
}
.jv-sched-select:focus-visible {
	outline: none;
	border-color: var(--outline-gray-4, #a3a3a3);
	box-shadow: 0 0 0 2px var(--outline-gray-2, #e5e5e5);
}
.jv-sched-select:disabled {
	cursor: not-allowed;
	opacity: 0.6;
}
.jv-sched-chip {
	height: 2rem;
	min-width: 3rem;
	padding: 0 0.625rem;
	border-radius: 0.5rem;
	border: 1px solid var(--outline-gray-2, #e2e2e2);
	background: var(--surface-white, #fff);
	color: var(--text-ink-gray-8, #383838);
	font-size: 13px;
	font-weight: 500;
	cursor: pointer;
}
.jv-sched-chip:hover {
	border-color: var(--outline-gray-4, #a3a3a3);
}
.jv-sched-chip.on {
	background: var(--surface-gray-7, #171717);
	border-color: var(--surface-gray-7, #171717);
	color: var(--text-ink-white, #fff);
}
.jv-sched-chip:focus-visible,
.jv-sched-quick:focus-visible,
.jv-sched-example:focus-visible {
	outline: 2px solid var(--outline-gray-3, #c7c7c7);
	outline-offset: 2px;
}
.jv-sched-chip:disabled {
	cursor: not-allowed;
	opacity: 0.6;
}
.jv-sched-dom {
	display: grid;
	grid-template-columns: repeat(7, 2.5rem);
	gap: 0.375rem;
}
.jv-sched-dom .jv-sched-chip {
	min-width: 0;
	width: 2.5rem;
	padding: 0;
}
.jv-sched-quick {
	border: 0;
	background: none;
	padding: 0;
	color: var(--text-ink-gray-6, #6b6b6b);
	text-decoration: underline;
	text-underline-offset: 3px;
	cursor: pointer;
}
.jv-sched-quick:hover {
	color: var(--text-ink-gray-9, #171717);
}
.jv-sched-cron {
	display: block;
	width: min(22rem, 100%);
	padding: 0.375rem 0.625rem;
	border: 1px solid transparent;
	border-radius: 0.5rem;
	background: var(--surface-gray-2, #f3f3f3);
	color: var(--text-ink-gray-9, #171717);
	font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	font-size: 14px;
	letter-spacing: 0.02em;
	resize: vertical;
}
.jv-sched-cron:hover {
	border-color: var(--outline-gray-2, #e2e2e2);
}
.jv-sched-cron:focus {
	outline: none;
	background: var(--surface-white, #fff);
	border-color: var(--outline-gray-4, #a3a3a3);
	box-shadow: 0 0 0 2px var(--outline-gray-2, #e5e5e5);
}
.jv-sched-legend {
	display: grid;
	grid-template-columns: repeat(5, minmax(0, 1fr));
	width: min(22rem, 100%);
	margin-top: 0.375rem;
	font-size: 11px;
	color: var(--text-ink-gray-5, #6b6b6b);
	text-align: center;
}
.jv-sched-example {
	height: 1.625rem;
	padding: 0 0.625rem;
	border-radius: 999px;
	border: 1px solid var(--outline-gray-2, #e2e2e2);
	background: var(--surface-white, #fff);
	color: var(--text-ink-gray-8, #383838);
	font-size: 12px;
	cursor: pointer;
}
.jv-sched-example:hover {
	border-color: var(--outline-gray-4, #a3a3a3);
}
.jv-sched-summary {
	display: flex;
	flex-direction: column;
	gap: 0.75rem;
	padding: 1rem 1.125rem;
	border: 1px solid var(--outline-gray-1, #ededed);
	border-radius: 0.75rem;
	background: var(--surface-gray-1, #fafafa);
}
.jv-sched-warn {
	margin: 0;
	padding: 0.5rem 0.625rem;
	border-radius: 0.5rem;
	background: var(--surface-amber-1, #fff7ed);
	color: var(--text-ink-amber-3, #9a4a0b);
	font-size: 13px;
}
.jv-sched-meta {
	display: flex;
	flex-direction: column;
	gap: 0.25rem;
	padding-top: 0.625rem;
	border-top: 1px solid var(--outline-gray-1, #ededed);
	font-size: 12px;
	color: var(--text-ink-gray-5, #6b6b6b);
}
.jv-sched-meta code {
	padding: 1px 5px;
	border-radius: 4px;
	background: var(--surface-gray-2, #f0f0f0);
	color: var(--text-ink-gray-8, #383838);
	font-size: 12px;
}
</style>
