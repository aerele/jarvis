<template>
	<section class="jv-document" aria-label="Document text and tables">
		<div class="jv-document-bar">
			<span>{{ __("Text and tables preview") }}</span>
			<a v-if="fileUrl" :href="fileUrl" download>{{ __("Download original") }}</a>
		</div>
		<div v-if="document.truncated" class="jv-document-note" role="status">
			{{ __("Preview is incomplete. Download the original for the full document.") }}
		</div>
		<div v-for="(note, i) in document.notes || []" :key="'note-' + i" class="jv-document-note">
			{{ __(note) }}
		</div>
		<template v-for="(block, i) in blocks" :key="i">
			<div
				v-if="
					block.part &&
					block.part !== blocks[i - 1]?.part &&
					(block.part !== 'body' || i > 0)
				"
				class="jv-document-part"
			>
				{{ __(PARTS[block.part] || "Document text") }}
			</div>
			<p v-if="block.type === 'paragraph'" class="jv-document-paragraph">{{ block.text }}</p>
			<div
				v-else-if="block.type === 'table'"
				class="jv-document-table"
				tabindex="0"
				:aria-label="__('Document table')"
			>
				<table>
					<tbody>
						<tr v-for="(row, ri) in block.rows || []" :key="ri">
							<td v-for="(value, ci) in row" :key="ci">{{ value }}</td>
						</tr>
					</tbody>
				</table>
			</div>
		</template>
	</section>
</template>

<script setup>
import { computed } from "vue";
import { __ } from "../lib/i18n";
const props = defineProps({
	document: { type: Object, default: () => ({}) },
	fileUrl: { type: String, default: "" },
});
const blocks = computed(() => (Array.isArray(props.document.blocks) ? props.document.blocks : []));
const PARTS = {
	header: "Header",
	body: "Body",
	footer: "Footer",
	footnotes: "Footnotes",
	endnotes: "Endnotes",
};
</script>

<style scoped>
/* Scoped CSS is intentional: the PWA's Tailwind scan excludes frontend/. */
.jv-document {
	min-width: 0;
	max-width: 100%;
	padding: 16px 20px;
	color: var(--text, var(--ink-gray-8, #1f2937));
	font: inherit;
}
.jv-document-bar {
	display: flex;
	flex-wrap: wrap;
	justify-content: space-between;
	gap: 8px 16px;
	margin-bottom: 16px;
	font-size: 12px;
	color: var(--text-2, var(--ink6, var(--ink-gray-6, #6b7280)));
}
.jv-document-bar a {
	color: var(--link, var(--ink-gray-8, #1f2937));
	text-decoration: underline;
	font-weight: 600;
}
.jv-document-note {
	margin: 8px 0;
	font-size: 12px;
	line-height: 1.5;
}
.jv-document-part {
	margin: 14px 0 6px;
	font-size: 12px;
	font-weight: 600;
}
.jv-document-paragraph {
	margin: 10px 0;
	white-space: pre-wrap;
	overflow-wrap: anywhere;
	line-height: 1.6;
}
.jv-document-table {
	max-width: 100%;
	overflow-x: auto;
	margin: 12px 0;
	border: 1px solid var(--border, var(--outline-gray-2, #d1d5db));
	border-radius: 6px;
}
.jv-document-table:focus-visible {
	outline: 2px solid var(--link, #1579d0);
	outline-offset: 2px;
}
.jv-document-table table {
	border-collapse: collapse;
	min-width: 100%;
	width: max-content;
	font-size: 13px;
}
.jv-document-table td {
	max-width: 28rem;
	padding: 8px 12px;
	border: 1px solid var(--border, var(--outline-gray-2, #d1d5db));
	white-space: pre-wrap;
	overflow-wrap: anywhere;
	vertical-align: top;
}
@media (max-width: 480px) {
	.jv-document {
		padding: 12px;
	}
}
</style>
