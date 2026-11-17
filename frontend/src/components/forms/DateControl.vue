<!-- A Date / Datetime / Time control drawn with frappe-ui's own pickers (the calendar
     and time list Desk users know), in place of the browser's <input type="date">,
     "datetime-local" and "time" (#833). It speaks the SAME value strings those
     inputs did, so every caller, its dirty check and its save path are unchanged:
       date      "YYYY-MM-DD"
       datetime  "YYYY-MM-DDTHH:mm"   (the picker itself takes "YYYY-MM-DD HH:mm:ss")
       time      "HH:mm"
     Empty is "". A value the picker cannot read is shown empty, as the native input did.
     frappe-ui's Tailwind classes do not style inputs in the chat (see #806), so the
     picker's input is drawn here to match its neighbours: `look="card"` is a card
     field (.jv-action-input), `look="ask"` a question-card field (.jv-ask-field). -->
<template>
	<!-- The wrapper carries the scoped style hook: frappe-ui's picker root does not. -->
	<div ref="root" :class="['jv-datectl', `jv-datectl--${look}`]">
		<DatePicker
			v-if="kind === 'date'"
			:modelValue="value"
			:disabled="disabled"
			:placeholder="placeholder || 'Pick a date'"
			@update:modelValue="(v) => emit('update:modelValue', toDate(v))"
		/>
		<DateTimePicker
			v-else-if="kind === 'datetime'"
			:modelValue="toPickerDatetime(value)"
			:disabled="disabled"
			:placeholder="placeholder || 'Pick a date and time'"
			@update:modelValue="(v) => emit('update:modelValue', fromPickerDatetime(v))"
		/>
		<TimePicker
			v-else
			:modelValue="toTime(value)"
			:disabled="disabled"
			:placeholder="placeholder || 'Pick a time'"
			@update:modelValue="(v) => emit('update:modelValue', toTime(v))"
		/>
	</div>
</template>

<script setup>
import { computed, normalizeClass, onMounted, onUpdated, ref, useAttrs } from "vue";
import { DatePicker, DateTimePicker, TimePicker } from "frappe-ui";
import {
	fromPickerDatetime,
	inputAttrsFor,
	inputClassesFor,
	toDate,
	toPickerDatetime,
	toTime,
} from "@/lib/dateControl";

// id / aria-* belong on the picker's <input>, not on the wrapper. They cannot be
// passed to the picker either: its root is frappe-ui's Popover (inheritAttrs:false,
// keeps only class and style), and `inputClass` lands on TextInput's wrapper div,
// not the input (TimePicker has no inputClass at all). So they are set on the real
// input after every render (applyToInput below).
defineOptions({ inheritAttrs: false });
const props = defineProps({
	// "date" | "datetime" | "time"
	kind: { type: String, required: true },
	modelValue: { type: String, default: "" },
	disabled: { type: Boolean, default: false },
	placeholder: { type: String, default: "" },
	// extra classes for the input, besides the look's own class
	inputClass: { type: [String, Array, Object], default: "" },
	ariaLabel: { type: String, default: "" },
	// "card" | "ask": which neighbouring input to look like. The input also gets
	// that input's class (.jv-action-input / .jv-ask-field), so the callers'
	// state rules (.missing, .invalid, .changed) reach it.
	look: { type: String, default: "card" },
});
const emit = defineEmits(["update:modelValue"]);
const value = computed(() => (props.modelValue == null ? "" : String(props.modelValue)));

const root = ref(null);
const attrs = useAttrs();
// The classes this control added last time, so a dropped one is removed again.
let added = [];

function applyToInput() {
	const input = root.value && root.value.querySelector("input");
	if (!input) return;
	// attrs is not reactive: read it here, on every render, never in a computed.
	for (const [name, val] of Object.entries(inputAttrsFor(attrs, props.ariaLabel))) {
		if (val == null) input.removeAttribute(name);
		else input.setAttribute(name, val);
	}
	const classes = inputClassesFor(props.look, normalizeClass(props.inputClass));
	for (const c of added) if (!classes.includes(c)) input.classList.remove(c);
	input.classList.add(...classes);
	added = classes;
}
// onUpdated runs after the picker's own patch, so a re-render that rewrites the
// input's class (a disabled toggle) is followed by putting ours back.
onMounted(applyToInput);
onUpdated(applyToInput);
</script>

<style scoped>
.jv-datectl {
	width: 100%;
}
.jv-datectl :deep(input) {
	width: 100%;
	height: auto;
	box-sizing: border-box;
	font-family: inherit;
	font-size: 13px;
	line-height: 1.45;
	color: var(--text);
	background: var(--surface);
	border: 1px solid var(--border-2);
	border-radius: 7px;
	padding: 6px 28px 6px 9px;
	outline: none;
	box-shadow: none;
	transition: border-color 0.12s, box-shadow 0.12s;
}
.jv-datectl :deep(input:focus) {
	border-color: var(--cta);
	box-shadow: 0 0 0 3px var(--cta-bg);
}
.jv-datectl--ask :deep(input) {
	padding: 8px 28px 8px 10px;
	background: var(--surface-2);
	border-color: var(--border);
	border-radius: 8px;
}
.jv-datectl--ask :deep(input:focus) {
	border-color: var(--cta);
	box-shadow: none;
}
</style>
