<template>
	<!-- The settings staple: title + help on the left, a real Switch on the right
	     (design.md §3.7). Replaces the hand-rolled .jv-switch button — §5
	     anti-pattern 9. The on-state is near-black, not green or blue; that comes
	     from frappe-ui's Switch and must not be overridden.

	     The label must describe the ON state so the switch is never inverted
	     (§5 anti-pattern 17). If a caller needs the negation, it inverts at the
	     binding, not here. -->
	<div class="flex items-start justify-between gap-4 py-3">
		<div class="flex flex-col gap-0.5">
			<span class="text-base font-medium text-ink-gray-8">{{ title }}</span>
			<span v-if="help" class="max-w-lg text-p-sm text-ink-gray-6">{{ help }}</span>
		</div>
		<Switch
			class="shrink-0"
			:modelValue="modelValue"
			:disabled="disabled"
			@update:modelValue="$emit('update:modelValue', $event)"
		/>
	</div>
</template>

<script setup>
import { Switch } from "frappe-ui";

defineProps({
	title: { type: String, required: true },
	help: { type: String, default: "" },
	modelValue: { type: Boolean, default: false },
	disabled: { type: Boolean, default: false },
});
defineEmits(["update:modelValue"]);
</script>
