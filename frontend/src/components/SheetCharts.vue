<template>
	<div v-if="charts.length" class="space-y-3 border-b p-3">
		<div v-for="(spec, ci) in charts" :key="ci" class="rounded bg-surface-white p-2">
			<JvChart :spec="spec" :dark="dark" />
		</div>
	</div>
</template>

<script setup>
// SheetCharts - the Excel charts above the current sheet's grid, drawn with the
// chat's JvChart. Used by FilePreview and the chat's artifact panel.
import JvChart from "@/charts/JvChart.vue";

defineProps({
	// already filtered to the sheet on screen (see chartsForSheet)
	charts: { type: Array, default: () => [] },
	dark: { type: Boolean, default: false },
});
</script>
