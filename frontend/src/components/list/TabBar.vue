<template>
	<div role="tablist" class="flex min-h-[45px] items-center gap-7.5 border-b px-5">
		<button
			v-for="tab in tabs"
			:key="tab.value"
			role="tab"
			:aria-selected="modelValue === tab.value"
			class="relative flex h-full min-h-[45px] items-center gap-1.5 text-base"
			:class="
				modelValue === tab.value
					? 'text-ink-gray-9'
					: 'text-ink-gray-5 hover:text-ink-gray-7'
			"
			@click="$emit('update:modelValue', tab.value)"
		>
			{{ tab.label }}
			<Badge
				v-if="tab.count != null"
				:label="String(tab.count)"
				theme="gray"
				variant="subtle"
				size="sm"
			/>
			<div
				v-if="modelValue === tab.value"
				class="absolute bottom-0 left-0 right-0 h-[2px] rounded-t bg-[color:var(--ink-gray-9)]"
			/>
		</button>
	</div>
</template>

<script setup>
// TabBar - underline tabs strip (DESIGN-V3 §5.5); used by MacrosList (B5) and
// AgentDetail (B6). Active tab = ink-gray-9 text + 2px underline bar.
import { Badge } from "frappe-ui";

defineProps({
	tabs: { type: Array, default: () => [] }, // [{label, value, count?}]
	modelValue: { type: [String, Number], default: "" },
});

defineEmits(["update:modelValue"]);
</script>
