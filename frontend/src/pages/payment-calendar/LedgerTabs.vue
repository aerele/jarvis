<template>
	<div v-if="tabs.length" class="ledger-tabs" role="group" aria-label="Invoice side">
		<button
			v-for="tab in tabs"
			:key="tab.side"
			type="button"
			:data-modal-side="prefix === 'modal' ? tab.side : undefined"
			:data-panel-side="prefix === 'panel' ? tab.side : undefined"
			:aria-pressed="String(side === tab.side)"
			:class="{ active: side === tab.side }"
			@click="emit('select', tab.side)"
		>
			<span
				>{{ tab.label }} <span class="count">{{ tab.count }}</span></span
			>
			<strong>{{ tab.total }}</strong>
		</button>
	</div>
</template>
<script setup>
import "./tokens.css";
import "./LedgerTabs.css";

// The "Invoice side" tabs shared by the side panel and the full-details dialog.
defineProps({
	tabs: { type: Array, required: true },
	side: { type: String, required: true },
	// Selects the data-*-side attribute the browser checks rely on.
	prefix: { type: String, default: "panel" },
});
const emit = defineEmits(["select"]);
</script>
