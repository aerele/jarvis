<script setup>
// A row's open form in the record draft panel: the titled block below the grid that
// holds DraftRowForm. It is focusable (tabindex -1) so opening it can move focus in,
// and Escape or Done asks the panel to close it; the panel returns focus to the row's
// toggle (lib/rowFormFocus.js). Escape is handled here, on the block, not globally:
// a Link input with its suggestion menu open closes only that menu (DraftCell).
import { computed, ref } from "vue";
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
const emit = defineEmits([
	"close",
	"remove",
	"insert",
	"link-search",
	"link-blur",
	"link-pick",
	"link-close",
]);

// The row's first grid value (its item, usually), to name it above its form.
const title = computed(() => {
	const cols = props.table.columns || [];
	const c = cols.find((x) => !x.read_only) || cols[0];
	return c ? String(props.row[c.fieldname] ?? "").trim() : "";
});

// The form, for Expand all / Collapse all (DraftRowForm exposes its folding).
const form = ref(null);
const canFold = computed(() => !!form.value?.canFold);
const allOpen = computed(() => !!form.value?.allOpen);

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
		<!-- As Desk's row editor: which row, and its actions (fold all, insert a row
		     below, delete, done). -->
		<div class="jv-rowform-head">
			<span :id="titleId" class="jv-rowform-title"
				>Editing row {{ rowIndex + 1
				}}<template v-if="title"> · {{ title }}</template></span
			>
			<span class="jv-rowform-acts">
				<button
					v-if="canFold"
					type="button"
					class="jv-rowform-btn jv-rowform-fold"
					:aria-label="
						(allOpen ? 'Collapse' : 'Expand') +
						' all sections of row ' +
						(rowIndex + 1)
					"
					@click="form.setAllOpen(!allOpen)"
				>
					{{ allOpen ? "Collapse all" : "Expand all" }}
				</button>
				<button
					type="button"
					class="jv-rowform-btn jv-rowform-insert"
					:aria-label="'Insert a row below row ' + (rowIndex + 1)"
					@click="emit('insert')"
				>
					<svg
						width="14"
						height="14"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
						aria-hidden="true"
					>
						<path d="M5 12h14" />
						<path d="M12 5v14" />
					</svg>
					<span class="jv-rowform-btn-text">Insert below</span>
				</button>
				<button
					type="button"
					class="jv-rowform-btn jv-rowform-remove"
					:aria-label="'Delete row ' + (rowIndex + 1)"
					@click="emit('remove')"
				>
					<svg
						width="14"
						height="14"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
						aria-hidden="true"
					>
						<path d="M3 6h18" />
						<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
						<path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
					</svg>
					<span class="jv-rowform-btn-text">Delete</span>
				</button>
				<button type="button" class="jv-rowform-close" @click="emit('close')">Done</button>
			</span>
		</div>
		<DraftRowForm
			ref="form"
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
/* A row's full form, opened in place of its grid row (ChatView pins it to the panel's
   width). An accent edge marks the row being edited. */
.jv-rowform-block {
	box-shadow: inset 3px 0 0 var(--cta);
}
.jv-rowform-block:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: -2px;
}
.jv-rowform-head {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	justify-content: space-between;
	gap: 8px;
	padding: 8px 10px 8px 14px;
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
/* The card is a size container: on a narrow card (a phone) Insert below and Delete
   show their icon only (their aria-label still names them), so the header keeps to
   one line. */
.jv-rowform-block {
	container-type: inline-size;
}
@container (max-width: 480px) {
	.jv-rowform-btn-text {
		display: none;
	}
	.jv-rowform-btn {
		padding: 0 8px;
	}
}
.jv-rowform-acts {
	display: flex;
	flex-wrap: wrap;
	justify-content: flex-end;
	align-items: center;
	gap: 6px;
}
/* The panel's secondary button (.jv-action-2nd), compact. */
.jv-rowform-btn {
	display: inline-flex;
	align-items: center;
	gap: 5px;
	min-height: 32px;
	padding: 0 10px;
	font: inherit;
	font-size: 12.5px;
	font-weight: 500;
	color: var(--text-2);
	background: var(--surface);
	border: 1px solid var(--border-2);
	border-radius: 8px;
	cursor: pointer;
}
.jv-rowform-btn:hover {
	background: var(--surface-1);
	color: var(--text);
}
.jv-rowform-remove {
	color: var(--red);
}
.jv-rowform-remove:hover {
	color: var(--red);
	border-color: var(--red);
}
/* The way out, as the panel's primary button. */
.jv-rowform-close {
	min-height: 32px;
	padding: 0 14px;
	font: inherit;
	font-size: 12.5px;
	font-weight: 600;
	color: var(--cta-fg);
	background: var(--cta);
	border: 1px solid var(--cta);
	border-radius: 8px;
	cursor: pointer;
}
.jv-rowform-btn:focus-visible,
.jv-rowform-close:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 2px;
}
</style>
