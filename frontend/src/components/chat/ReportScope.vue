<template>
	<div v-if="scopes.length" class="report-scopes" role="group" aria-label="Reports consulted">
		<span class="report-scope-title">Reports consulted</span>
		<div v-for="scope in scopes" :key="scope.id" class="report-scope">
			<span class="report-scope-title">{{ scope.report_name }}</span>
			<span>{{ scope.company }} · As of {{ scope.report_date }} · {{ scope.currency }}</span>
			<span v-if="scope.generated_at">Generated: {{ scope.generated_at }}</span>
			<span v-if="scope.row_note">{{ scope.row_note }}</span>
		</div>
	</div>
</template>

<script setup>
import { computed } from "vue";
import { reportScopes } from "@/lib/reportScope";

const props = defineProps({ tools: { type: Array, default: () => [] } });
const scopes = computed(() => reportScopes(props.tools));
</script>

<style scoped>
.report-scopes {
	display: grid;
	gap: 6px;
	margin-bottom: 12px;
}
.report-scope {
	display: grid;
	gap: 3px;
	border-left: 2px solid var(--border-2);
	padding: 3px 10px;
	font-size: 12px;
	line-height: 1.5;
	overflow-wrap: anywhere;
}
.report-scope-title {
	font-weight: 600;
}
</style>
