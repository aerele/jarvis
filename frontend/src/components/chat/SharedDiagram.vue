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
				// Preserve Mermaid's default protected keys and prevent message
				// directives/front-matter changing styles or SVG label rendering.
				secure: [
					"secure",
					"securityLevel",
					"startOnLoad",
					"maxTextSize",
					"suppressErrorRendering",
					"maxEdges",
					"themeCSS",
					"htmlLabels",
					"flowchart",
					"fontFamily",
					"altFontFamily",
					"themeVariables",
				],
				htmlLabels: false,
				flowchart: { htmlLabels: false },
				theme: props.dark ? "dark" : "default",
			});
			const result = await mermaid.render(
				"shared-diagram-" + ++diagramSequence,
				props.source || ""
			);
			if (gen === generation) {
				const clean = DOMPurify.sanitize(result.svg, {
					USE_PROFILES: { svg: true, svgFilters: true },
					FORBID_TAGS: ["foreignObject", "a"],
					FORBID_ATTR: ["href", "xlink:href"],
				});
				const document = new DOMParser().parseFromString(clean, "image/svg+xml");
				if ([...document.querySelectorAll("text")].some((node) => node.textContent.trim()))
					svg.value = clean;
			}
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
