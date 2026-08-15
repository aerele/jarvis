<template>
	<div v-if="!option" class="jv-chart-bad">No data to display.</div>
	<div v-else ref="el" class="jv-echart"></div>
</template>

<script setup>
import { ref, watch, onMounted, onBeforeUnmount, nextTick } from "vue";
const props = defineProps({ option: { type: Object, default: null } });
const el = ref(null);
let chart = null,
	ro = null;

async function ensure() {
	if (!props.option || !el.value) return;
	if (!chart) {
		const echarts = await import("echarts");
		if (!el.value) return;
		chart = echarts.init(el.value, null, { renderer: "svg" });
		ro = new ResizeObserver(() => chart && chart.resize());
		ro.observe(el.value);
	}
	chart.setOption(props.option, true);
}
onMounted(() => nextTick(ensure));
watch(() => props.option, ensure);
onBeforeUnmount(() => {
	if (ro && el.value) ro.unobserve(el.value);
	if (chart) {
		chart.dispose();
		chart = null;
	}
});
</script>

<style scoped>
.jv-echart {
	width: 100%;
	height: 200px;
}
.jv-chart-bad {
	font-size: 13px;
	color: var(--text-3);
	padding: 8px 0;
}
</style>
