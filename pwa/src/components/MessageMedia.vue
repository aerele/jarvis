<script setup>
import CanvasFrame from "./CanvasFrame.vue";
import { fileExt, previewKind } from "../lib/canvas";

// A message's `canvas` items: attached images, generated images, and agent
// artifacts (charts, diagrams, PDFs, spreadsheets). The PWA used to ignore this
// field entirely, so anything the agent produced other than prose simply never
// appeared.
//
// Images render inline. html/svg artifacts render inline as a sandboxed frame.
// Everything else becomes a chip that opens the preview sheet — a 4 MB xlsx has
// no business auto-loading itself on mobile data.
const props = defineProps({
	items: { type: Array, default: () => [] },
	messageName: { type: String, required: true },
});
const emit = defineEmits(["open"]);
</script>

<template>
	<template v-for="(item, i) in props.items" :key="item.name || item.file_url || i">
		<button v-if="previewKind(item) === 'image'" class="jv-thumb" @click="emit('open', item)">
			<!-- Same-origin: the session cookie authenticates the request, so a
			     private file just works. (The native app has to attach headers.) -->
			<img :src="item.file_url" :alt="item.title || 'Image'" loading="lazy" />
			<span class="jv-thumb-tag">
				<svg
					viewBox="0 0 24 24"
					width="11"
					height="11"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
					stroke-linejoin="round"
				>
					<path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7" />
				</svg>
				{{ item.title || "Image" }}
			</span>
		</button>

		<CanvasFrame
			v-else-if="previewKind(item) === 'html' || previewKind(item) === 'svg'"
			:message-name="props.messageName"
			:canvas-name="item.name"
		/>

		<button v-else class="jv-artifact" @click="emit('open', item)">
			<span class="jv-artifact-icon">
				<svg
					viewBox="0 0 24 24"
					width="17"
					height="17"
					fill="none"
					stroke="currentColor"
					stroke-width="1.8"
					stroke-linecap="round"
					stroke-linejoin="round"
				>
					<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
					<path d="M14 2v6h6" />
				</svg>
			</span>
			<span class="jv-artifact-main">
				<span class="jv-artifact-title">{{ item.title || "Attachment" }}</span>
				<span class="jv-artifact-sub">{{ fileExt(item) }} · open preview</span>
			</span>
			<svg
				class="jv-artifact-chev"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<path d="m9 18 6-6-6-6" />
			</svg>
		</button>
	</template>
</template>

<style scoped>
.jv-thumb {
	display: block;
	position: relative;
	margin-top: 8px;
	max-width: 240px;
	padding: 0;
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--card2);
	overflow: hidden;
	cursor: pointer;
}
.jv-thumb img {
	display: block;
	width: 240px;
	height: 180px;
	object-fit: cover;
}
.jv-thumb-tag {
	position: absolute;
	left: 8px;
	bottom: 8px;
	display: inline-flex;
	align-items: center;
	gap: 5px;
	max-width: 224px;
	padding: 4px 8px;
	border-radius: 999px;
	background: rgba(0, 0, 0, 0.55);
	color: #fff;
	font-size: 11px;
	font-weight: 500;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-artifact {
	display: flex;
	align-items: center;
	gap: 11px;
	width: 100%;
	margin-top: 8px;
	padding: 11px;
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--card);
	font: inherit;
	text-align: left;
	cursor: pointer;
}
.jv-artifact-icon {
	display: grid;
	place-items: center;
	width: 34px;
	height: 34px;
	flex: none;
	border-radius: 9px;
	background: var(--card2);
	color: var(--ink6);
}
.jv-artifact-main {
	flex: 1;
	min-width: 0;
	display: flex;
	flex-direction: column;
}
.jv-artifact-title {
	font-size: 13px;
	font-weight: 600;
	color: var(--ink9);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-artifact-sub {
	font-size: 11px;
	color: var(--ink5);
}
.jv-artifact-chev {
	width: 16px;
	height: 16px;
	flex: none;
	color: var(--ink4);
}
</style>
