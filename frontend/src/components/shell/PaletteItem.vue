<template>
	<div
		class="flex h-9 cursor-pointer items-center gap-2 rounded px-2.5 text-base text-ink-gray-8"
		:class="{ 'bg-surface-gray-2': active }"
	>
		<FeatherIcon v-if="item.icon" :name="item.icon" class="size-4 shrink-0 text-ink-gray-5" />
		<span class="truncate">{{ item.label }}</span>
		<span v-if="suffix" class="ml-auto shrink-0 text-sm text-ink-gray-4">{{ suffix }}</span>
	</div>
</template>

<script setup>
// Command-palette result row (DESIGN-V3 §3.5): rendered by
// JarvisCommandPalette's grouped list. Chats show a timeAgo suffix.
import { computed } from "vue";
import { FeatherIcon } from "frappe-ui";
import { timeAgo } from "@/utils/datetime";

const props = defineProps({
	item: { type: Object, required: true },
	active: { type: Boolean, default: false },
});

// Frappe rows carry an explicit suffix (the doctype/"List"/"Report" tag); chat
// rows fall back to a relative-time stamp.
const suffix = computed(() => props.item.suffix ?? timeAgo(props.item.last_active_at));
</script>
