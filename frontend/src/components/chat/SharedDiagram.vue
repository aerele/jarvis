<template>
	<div class="shared-diagram overflow-x-auto">
		<div v-if="svg" v-html="svg" />
		<pre v-else class="whitespace-pre-wrap break-words text-sm">{{ source }}</pre>
	</div>
</template>
<script>
let diagramSequence = 0;
</script>
<script setup>
import { ref, watch, onBeforeUnmount } from "vue";
import DOMPurify from "dompurify";
const props = defineProps({ source: String, dark: Boolean });
const svg = ref("");
let generation = 0;
watch(
	() => [props.source, props.dark],
	async () => {
		const gen = ++generation;
		svg.value = "";
		try {
			const { default: mermaid } = await import("mermaid");
			if (gen !== generation) return;
			mermaid.initialize({
				startOnLoad: false,
				securityLevel: "strict",
				htmlLabels: false,
				flowchart: { htmlLabels: false },
				theme: props.dark ? "dark" : "default",
			});
			const result = await mermaid.render(
				"shared-diagram-" + ++diagramSequence,
				props.source || ""
			);
			if (gen === generation)
				svg.value = DOMPurify.sanitize(result.svg, {
					USE_PROFILES: { svg: true, svgFilters: true },
					FORBID_TAGS: ["foreignObject", "a"],
					FORBID_ATTR: ["href", "xlink:href"],
				});
		} catch {
			/* Keep the escaped source when a diagram is malformed. */
		}
	},
	{ immediate: true }
);
onBeforeUnmount(() => ++generation);
</script>
<style scoped>
.shared-diagram :deep(svg) {
	max-width: 100%;
	height: auto;
}
</style>
