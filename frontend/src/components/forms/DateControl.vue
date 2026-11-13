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
	<div :class="['jv-datectl', `jv-datectl--${look}`]">
		<DatePicker
			v-if="kind === 'date'"
			v-bind="$attrs"
			:modelValue="value"
			:disabled="disabled"
			:placeholder="placeholder || 'Pick a date'"
			:inputClass="inputClass"
			:aria-label="ariaLabel || undefined"
			@update:modelValue="(v) => emit('update:modelValue', toDate(v))"
		/>
		<DateTimePicker
			v-else-if="kind === 'datetime'"
			v-bind="$attrs"
			:modelValue="toPickerDatetime(value)"
			:disabled="disabled"
			:placeholder="placeholder || 'Pick a date and time'"
			:inputClass="inputClass"
			:aria-label="ariaLabel || undefined"
			@update:modelValue="(v) => emit('update:modelValue', fromPickerDatetime(v))"
		/>
		<TimePicker
			v-else
			v-bind="$attrs"
			:modelValue="toTime(value)"
			:disabled="disabled"
			:placeholder="placeholder || 'Pick a time'"
			:aria-label="ariaLabel || undefined"
			@update:modelValue="(v) => emit('update:modelValue', toTime(v))"
		/>
	</div>
</template>

<script setup>
import { computed } from "vue";
import { DatePicker, DateTimePicker, TimePicker } from "frappe-ui";
import { toDate, toTime, toPickerDatetime, fromPickerDatetime } from "@/lib/dateControl";

// id / aria-* go on the picker, not the wrapper
defineOptions({ inheritAttrs: false });
const props = defineProps({
	// "date" | "datetime" | "time"
	kind: { type: String, required: true },
	modelValue: { type: String, default: "" },
	disabled: { type: Boolean, default: false },
	placeholder: { type: String, default: "" },
	inputClass: { type: [String, Array, Object], default: "" },
	ariaLabel: { type: String, default: "" },
	// "card" | "ask": which neighbouring input to look like
	look: { type: String, default: "card" },
});
const emit = defineEmits(["update:modelValue"]);
const value = computed(() => (props.modelValue == null ? "" : String(props.modelValue)));
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
