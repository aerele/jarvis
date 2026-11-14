<template>
	<!-- The draft (desk form, new tab), "Open review" (in-app, reviewers only) or nothing. -->
	<a
		v-if="link && link.kind === 'desk'"
		:href="link.href"
		target="_blank"
		rel="noopener noreferrer"
		class="text-ink-blue-3 hover:underline"
		>{{ link.label }}</a
	>
	<router-link
		v-else-if="link"
		:to="link.href"
		class="text-ink-blue-3 hover:underline"
		:aria-label="`Open review of ${row.file_name || row.name}`"
		>{{ link.label }}</router-link
	>
</template>

<script setup>
import { computed } from "vue";
import { intakeDraftLink } from "@/lib/intakeStatus";

const props = defineProps({ row: { type: Object, required: true } });
const link = computed(() => intakeDraftLink(props.row));
</script>
