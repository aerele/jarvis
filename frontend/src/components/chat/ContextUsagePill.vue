<template>
	<span
		class="inline-flex h-[30px] shrink-0 items-center gap-1.5 rounded-full px-2.5 text-xs font-medium"
		:class="toneClass"
		:title="view.title"
		:aria-label="view.title"
	>
		<FeatherIcon name="pie-chart" class="size-3.5" />
		{{ view.label }}
	</span>
</template>

<script setup>
import { computed } from "vue";
import { FeatherIcon } from "frappe-ui";
import { contextUsageView } from "@/lib/contextUsage";

const props = defineProps({
	usage: { type: Object, default: () => ({}) },
});

const view = computed(() => contextUsageView(props.usage));
const toneClass = computed(() => {
	if (view.value.tone === "critical") return "bg-surface-red-2 text-ink-red-3";
	if (view.value.tone === "warning") return "bg-surface-amber-2 text-ink-amber-3";
	return "bg-surface-gray-2 text-ink-gray-6";
});
</script>
