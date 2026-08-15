<script setup>
import { onMounted, ref, watch } from "vue";
import * as api from "../api";

// An agent-generated html/svg artifact (a chart, a diagram), rendered inline.
//
// It goes in a sandboxed iframe with `srcdoc`: the body is model-authored
// markup, so it must not be able to touch this document, read the session, or
// call the API. `sandbox` with no `allow-same-origin` gives it a null origin —
// scripts may run (an ECharts artifact needs them) but they run in a box.
const props = defineProps({
	messageName: { type: String, required: true },
	canvasName: { type: String, default: "" },
	height: { type: Number, default: 260 },
});

const html = ref("");
const loading = ref(true);
const failed = ref(false);

const isDark = () => (window.matchMedia("(prefers-color-scheme: dark)").matches ? 1 : 0);

async function load() {
	loading.value = true;
	failed.value = false;
	try {
		const d = await api.getCanvas(props.messageName, props.canvasName, isDark());
		if (d?.content) {
			html.value = d.content;
		} else if (d?.data_url) {
			html.value = `<!doctype html><body style="margin:0;background:transparent;display:flex;align-items:center;justify-content:center"><img src="${d.data_url}" style="max-width:100%;max-height:100%"></body>`;
		} else {
			failed.value = true;
		}
	} catch (e) {
		console.error("Jarvis PWA: failed to load canvas", e);
		failed.value = true;
	} finally {
		loading.value = false;
	}
}

onMounted(load);
watch(() => [props.messageName, props.canvasName], load);
</script>

<template>
	<div class="jv-canvas" :style="{ height: `${props.height}px` }">
		<div v-if="loading" class="jv-canvas-state"><span class="jv-spinner" /></div>
		<div v-else-if="failed" class="jv-canvas-state">Couldn't load this chart.</div>
		<iframe
			v-else
			:srcdoc="html"
			sandbox="allow-scripts"
			referrerpolicy="no-referrer"
			title="Chart"
			loading="lazy"
		/>
	</div>
</template>

<style scoped>
.jv-canvas {
	margin-top: 8px;
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--card);
	overflow: hidden;
}
.jv-canvas iframe {
	display: block;
	width: 100%;
	height: 100%;
	border: 0;
	background: transparent;
}
.jv-canvas-state {
	display: grid;
	place-items: center;
	height: 100%;
	font-size: 12px;
	color: var(--ink5);
}
.jv-spinner {
	width: 18px;
	height: 18px;
	border-radius: 50%;
	border: 2px solid var(--card3);
	border-top-color: var(--accent);
	animation: jv-spin 0.7s linear infinite;
}
@keyframes jv-spin {
	to {
		transform: rotate(360deg);
	}
}
</style>
