<template>
	<div data-filebox-switch class="shrink-0">
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
