<script setup>
import { ref, watch } from "vue";
import Sheet from "./Sheet.vue";
import CanvasFrame from "./CanvasFrame.vue";
import * as api from "../api";
import { fileExt, previewKind } from "../lib/canvas";

// Tap an artifact → see it, without leaving the chat. Spreadsheets and text
// files come back already rendered by the bench (preview_file), so the phone
// never downloads and parses an xlsx.
const props = defineProps({
	item: { type: Object, default: null },
	messageName: { type: String, default: "" },
});
const emit = defineEmits(["close"]);

const preview = ref(null);
const loading = ref(false);
const error = ref("");
const sheetIdx = ref(0);

const kind = () => (props.item ? previewKind(props.item) : "file");

watch(
	() => props.item?.file_url,
	async (url) => {
		preview.value = null;
		error.value = "";
		sheetIdx.value = 0;
		// Images, PDFs and canvases render straight from their URL; only the
		// tabular/text kinds need the server to turn bytes into something a phone
		// can show.
		if (!url || ["image", "pdf", "html", "svg"].includes(kind())) return;
		loading.value = true;
		try {
			preview.value = await api.previewFile(url);
		} catch (e) {
			error.value = e?.message || "Couldn't open this file.";
		} finally {
			loading.value = false;
		}
	},
	{ immediate: true }
);
</script>

<template>
	<Sheet :open="!!props.item" @close="emit('close')">
		<div v-if="props.item" class="jv-preview">
			<div class="jv-preview-head">
				<div class="jv-preview-title">
					{{ props.item.title || props.item.name || "Attachment" }}
				</div>
				<a
					class="jv-preview-dl"
					:href="props.item.file_url"
					download
					target="_blank"
					rel="noopener"
				>
					<svg
						viewBox="0 0 24 24"
						width="17"
						height="17"
						fill="none"
						stroke="currentColor"
						stroke-width="1.9"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" />
					</svg>
				</a>
				<button class="jv-icon-btn" aria-label="Close" @click="emit('close')">
					<svg
						viewBox="0 0 24 24"
						width="18"
						height="18"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
					>
						<path d="M18 6 6 18M6 6l12 12" />
					</svg>
				</button>
			</div>

			<div class="jv-preview-body">
				<img
					v-if="kind() === 'image'"
					class="jv-preview-img"
					:src="props.item.file_url"
					:alt="props.item.title || 'Image'"
				/>

				<iframe
					v-else-if="kind() === 'pdf'"
					class="jv-preview-pdf"
					:src="props.item.file_url"
					title="PDF"
				/>

				<CanvasFrame
					v-else-if="kind() === 'html' || kind() === 'svg'"
					:message-name="props.messageName"
					:canvas-name="props.item.name"
					:height="420"
				/>

				<div v-else-if="loading" class="jv-preview-state"><span class="jv-spinner" /></div>
				<div v-else-if="error" class="jv-preview-state is-error">{{ error }}</div>

				<template v-else-if="preview?.kind === 'table'">
					<div v-if="(preview.sheets || []).length > 1" class="jv-tabs">
						<button
							v-for="(s, i) in preview.sheets"
							:key="i"
							class="jv-tab"
							:class="{ 'is-on': sheetIdx === i }"
							@click="sheetIdx = i"
						>
							{{ s.name || `Sheet ${i + 1}` }}
						</button>
					</div>
					<!-- Wide sheets scroll inside this box; the sheet itself never
					     scrolls sideways. -->
					<div class="jv-tablewrap">
						<table class="jv-table">
							<tbody>
								<tr
									v-for="(row, ri) in preview.sheets?.[sheetIdx]?.rows || []"
									:key="ri"
								>
									<td v-for="(cell, ci) in row" :key="ci">{{ cell }}</td>
								</tr>
							</tbody>
						</table>
					</div>
				</template>

				<pre v-else-if="preview?.kind === 'text'" class="jv-preview-text">{{
					preview.text
				}}</pre>

				<div v-else class="jv-preview-state">
					<div>{{ fileExt(props.item) }} file. No inline preview.</div>
					<a
						class="jv-preview-open"
						:href="props.item.file_url"
						download
						target="_blank"
						rel="noopener"
						>Download</a
					>
				</div>
			</div>
		</div>
	</Sheet>
</template>

<style scoped>
.jv-preview {
	display: flex;
	flex-direction: column;
	min-height: 0;
}
.jv-preview-head {
	display: flex;
	align-items: center;
	gap: 6px;
	padding: 2px 8px 10px 16px;
	border-bottom: 1px solid var(--border);
	flex: none;
}
.jv-preview-title {
	flex: 1;
	min-width: 0;
	font-size: 14.5px;
	font-weight: 600;
	color: var(--ink9);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-preview-dl {
	display: grid;
	place-items: center;
	width: 38px;
	height: 38px;
	flex: none;
	border-radius: 10px;
	color: var(--ink7);
}
.jv-preview-body {
	flex: 1;
	min-height: 0;
	overflow-y: auto;
	padding: 12px;
}
.jv-preview-img {
	display: block;
	max-width: 100%;
	margin: 0 auto;
	border-radius: 10px;
}
.jv-preview-pdf {
	width: 100%;
	height: 62dvh;
	border: 0;
	border-radius: 10px;
	background: var(--card2);
}
.jv-preview-state {
	display: flex;
	flex-direction: column;
	align-items: center;
	gap: 12px;
	padding: 40px 16px;
	font-size: 13px;
	color: var(--ink5);
	text-align: center;
}
.jv-preview-state.is-error {
	color: var(--red);
}
.jv-preview-open {
	padding: 10px 18px;
	border-radius: 10px;
	background: var(--accent-solid);
	color: #fff;
	font-size: 14px;
	font-weight: 600;
	text-decoration: none;
}
.jv-preview-text {
	margin: 0;
	font-size: 12.5px;
	line-height: 1.5;
	color: var(--ink8);
	white-space: pre-wrap;
	overflow-wrap: anywhere;
}
.jv-tabs {
	display: flex;
	gap: 6px;
	margin-bottom: 10px;
	overflow-x: auto;
}
.jv-tab {
	flex: none;
	padding: 6px 12px;
	border: 1px solid var(--border);
	border-radius: 999px;
	background: var(--card);
	color: var(--ink6);
	font: inherit;
	font-size: 12.5px;
	cursor: pointer;
}
.jv-tab.is-on {
	background: var(--accent-bg);
	border-color: transparent;
	color: var(--accent);
	font-weight: 600;
}
.jv-tablewrap {
	overflow-x: auto;
	border: 1px solid var(--border);
	border-radius: 10px;
}
.jv-table {
	border-collapse: collapse;
	font-size: 12px;
}
.jv-table td {
	padding: 7px 10px;
	border-bottom: 1px solid var(--border);
	border-right: 1px solid var(--border);
	color: var(--ink8);
	white-space: nowrap;
}
.jv-table tr:first-child td {
	background: var(--card2);
	font-weight: 600;
	color: var(--ink9);
	position: sticky;
	top: 0;
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
