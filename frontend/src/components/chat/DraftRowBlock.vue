<script setup>
// A row's open form in the record draft panel: the titled block below the grid that
// holds DraftRowForm. It is focusable (tabindex -1) so opening it can move focus in,
// and Escape or Done asks the panel to close it; the panel returns focus to the row's
// toggle (lib/rowFormFocus.js). Escape is handled here, on the block, not globally:
// a Link input with its suggestion menu open closes only that menu (DraftCell).
import { computed } from "vue";
import DraftRowForm from "@/components/chat/DraftRowForm.vue";
import { rowFormId } from "@/lib/rowFormFocus";

const props = defineProps({
	table: { type: Object, required: true },
	row: { type: Object, required: true },
	parent: { type: Object, default: () => ({}) },
	tableIndex: { type: Number, required: true },
	rowIndex: { type: Number, required: true },
	changedFields: { type: Object, default: () => new Set() },
	savedRow: { type: Object, default: null },
	draftLink: { type: Object, required: true },
	palette: { type: Object, default: () => ({}) },
});
const emit = defineEmits(["close", "link-search", "link-blur", "link-pick", "link-close"]);

// The row's first grid value (its item, usually), to name it above its form.
const title = computed(() => {
	const cols = props.table.columns || [];
	const c = cols.find((x) => !x.read_only) || cols[0];
	return c ? String(props.row[c.fieldname] ?? "").trim() : "";
});

// The visible title names the section for a screen reader.
const titleId = computed(() => `${rowFormId(props.tableIndex, props.row.__key)}-title`);

// This Escape is the block's: it must not also close the artifact panel (the page's
// own handler skips an event that is already handled). An Escape that belongs to an IME
// composition cancels the composition, not the form.
function onEscape(ev) {
	if (ev.isComposing || ev.keyCode === 229) return;
	ev.preventDefault();
	ev.stopPropagation();
	emit("close");
}
</script>

<template>
	<section
		:id="rowFormId(tableIndex, row.__key)"
		class="jv-rowform-block"
		tabindex="-1"
		:aria-labelledby="titleId"
		@keydown.esc="onEscape"
	>
		<div class="jv-rowform-head">
			<span :id="titleId" class="jv-rowform-title"
				>Row {{ rowIndex + 1 }}<template v-if="title"> · {{ title }}</template></span
			>
			<button type="button" class="jv-rowform-close" @click="emit('close')">Done</button>
		</div>
		<DraftRowForm
			:table="table"
			:row="row"
			:parent="parent"
			:table-index="tableIndex"
			:row-index="rowIndex"
			:changed-fields="changedFields"
			:saved-row="savedRow"
			:draft-link="draftLink"
			:palette="palette"
			@link-search="(...a) => emit('link-search', ...a)"
			@link-blur="(e) => emit('link-blur', e)"
			@link-pick="(...a) => emit('link-pick', ...a)"
			@link-close="emit('link-close')"
		/>
	</section>
</template>

<style scoped>
/* A row's full form, below the grid's sideways scroller so it fits the panel. */
.jv-rowform-block {
	margin-top: 8px;
	border: 1px solid var(--border);
	border-radius: 9px;
	overflow: hidden;
}
.jv-rowform-block:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 1px;
}
.jv-rowform-head {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 8px;
	padding: 6px 8px 6px 12px;
	background: var(--surface-1);
	border-bottom: 1px solid var(--border);
}
.jv-rowform-title {
	font-size: 12px;
	font-weight: 650;
	color: var(--text-2);
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-rowform-close {
	background: none;
	border: none;
	color: var(--cta);
	font-weight: 600;
	font-size: 12.5px;
	cursor: pointer;
	padding: 4px 6px;
	min-height: 32px;
}
.jv-rowform-close:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 1px;
}
</style>
