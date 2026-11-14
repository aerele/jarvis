<template>
	<div data-filebox-switch class="filebox-switch shrink-0">
		<TabButtons
			:buttons="VIEWS"
			:model-value="modelValue"
			@update:model-value="(v) => v && v !== modelValue && $emit('update:modelValue', v)"
		/>
	</div>
</template>

<script setup>
// The two File Box views: files dropped by hand, and Document Intake files.
import { TabButtons } from "frappe-ui";

defineProps({ modelValue: { type: String, default: "dropped" } });
defineEmits(["update:modelValue"]);

const VIEWS = [
	{ label: "Dropped", value: "dropped" },
	{ label: "Intake", value: "intake" },
];
</script>

<style scoped>
/* frappe-ui's inactive option is 3.76:1 on its track: darker ink, this usage only */
.filebox-switch :deep([role="radio"][aria-checked="false"]) {
	color: var(--ink-gray-7) !important;
}
</style>
