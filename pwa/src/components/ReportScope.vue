<script setup>
import { computed } from "vue";
import { reportScopes } from "@shared/lib/reportScope.js";

// Company, as-of date and currency of the reports behind an answer, read from
// the durable tool receipts (never the reply text). Same derivation as the
// desktop ReportScope.
const props = defineProps({ tools: { type: Array, default: () => [] } });
const scopes = computed(() => reportScopes(props.tools));
</script>

<template>
	<div v-if="scopes.length" class="jv-scopes" role="group" aria-label="Reports consulted">
		<span class="jv-scope-name">Reports consulted</span>
		<div v-for="scope in scopes" :key="scope.id" class="jv-scope">
			<span class="jv-scope-name">{{ scope.report_name }}</span>
			<span>{{ scope.company }} · As of {{ scope.report_date }} · {{ scope.currency }}</span>
			<span v-if="scope.generated_at">Generated: {{ scope.generated_at }}</span>
			<span v-if="scope.row_note">{{ scope.row_note }}</span>
		</div>
	</div>
</template>

<style scoped>
.jv-scopes {
	display: grid;
	gap: 6px;
	margin-bottom: 10px;
}
.jv-scope {
	display: grid;
	gap: 2px;
	border-left: 2px solid var(--border);
	padding: 2px 10px;
	font-size: 11.5px;
	line-height: 1.4;
	color: var(--ink7);
	overflow-wrap: anywhere;
}
.jv-scope-name {
	font-weight: 600;
	font-size: 11.5px;
	color: var(--ink9);
}
</style>
