<template>
	<div v-if="invalid" class="jv-chart-bad">Couldn't render this chart.</div>
	<div v-else ref="el" class="jv-chart"></div>
</template>

<script setup>
import { ref, watch, onMounted, onBeforeUnmount, computed, nextTick } from "vue";
import { buildOption } from "./chartTheme.js";

const props = defineProps({
	spec: { type: Object, required: true },
	dark: { type: Boolean, default: false },
});

const el = ref(null);
let chart = null;
let ro = null;

const option = computed(() => buildOption(props.spec, props.dark));
const invalid = computed(() => option.value === null);

async function ensure() {
	if (invalid.value || !el.value) return;
	if (!chart) {
		// Lazy import: keep echarts off the first-paint path (mirrors mermaid).
		const echarts = await import("echarts");
		if (!el.value) return;
		chart = echarts.init(el.value, null, { renderer: "svg" });
		ro = new ResizeObserver(() => chart && chart.resize());
		ro.observe(el.value);
	}
	chart.setOption(option.value, true);
}

onMounted(() => nextTick(ensure));
watch(option, ensure);
onBeforeUnmount(() => {
	if (ro && el.value) ro.unobserve(el.value);
	if (chart) {
		chart.dispose();
		chart = null;
	}
});
</script>

<style scoped>
.jv-chart {
	width: 100%;
	height: 280px;
}
.jv-chart-bad {
	font-size: 13px;
	color: var(--text-3);
	padding: 8px 0;
}
</style>
