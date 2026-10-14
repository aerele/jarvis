<template>
	<!-- Who a skill is shared with, and the way to change it (ShareDialog). The owner
	     sees it in the side panel; a skill reviewer on a shared skill too (5c). -->
	<div class="jv-skill-shares">
		<div class="flex items-center justify-between">
			<div class="text-sm text-ink-gray-5">Shared with</div>
			<Button label="Manage" @click="emit('manage')" />
		</div>
		<div v-if="shares.length" class="mt-2 flex items-center">
			<Tooltip v-for="u in shares.slice(0, 8)" :key="u.name" :text="u.full_name || u.name">
				<Avatar
					size="md"
					:label="u.full_name || u.name"
					class="-mr-1.5 ring-2 ring-outline-white hover:z-10 hover:scale-110"
				/>
			</Tooltip>
			<span v-if="shares.length > 8" class="ml-3 text-sm text-ink-gray-5">
				+{{ shares.length - 8 }}
			</span>
		</div>
		<div v-else class="mt-2 text-sm text-ink-gray-4">Not shared</div>
	</div>
</template>

<script setup>
import { Avatar, Button, Tooltip } from "frappe-ui";

defineProps({
	shares: { type: Array, default: () => [] }, // [{name, full_name}]
});
const emit = defineEmits(["manage"]);
</script>
