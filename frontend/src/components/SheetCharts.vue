<template>
	<div v-if="charts.length" class="space-y-3 border-b p-3">
		<template v-for="(spec, ci) in charts" :key="ci">
			<div v-if="captionAt(ci)" class="text-xs text-ink-gray-5">{{ spec.sheet }}</div>
			<div class="rounded bg-surface-white p-2">
				<JvChart :spec="spec" :dark="dark" />
			</div>
		</template>
	</div>
</template>

<script setup>
// SheetCharts - the Excel charts above the current sheet's grid, drawn with the
// chat's JvChart. Used by FilePreview and the chat's artifact panel.
import JvChart from "@/charts/JvChart.vue";

const props = defineProps({
	// already filtered to the sheet on screen (see chartsForSheet)
	charts: { type: Array, default: () => [] },
	// the sheet on screen: charts from other sheets get a caption with their own name
	sheetName: { type: String, default: "" },
	dark: { type: Boolean, default: false },
});

// the caption of a foreign sheet's charts is shown once, above its first chart
function captionAt(i) {
	const sheet = props.charts[i].sheet;
	return (
		!!sheet &&
		!!props.sheetName &&
		sheet !== props.sheetName &&
		props.charts[i - 1]?.sheet !== sheet
	);
}
</script>
