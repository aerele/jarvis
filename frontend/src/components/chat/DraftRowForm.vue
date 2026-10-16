<script setup>
// A child row's full form in the record draft panel: every field Desk's row edit
// form shows for this row, in its sections and columns, with depends_on and its
// siblings evaluated as the row changes (lib/rowForm.js). The grid shows a few
// columns; this shows the rest, so nothing the record would save is out of reach.
import { computed } from "vue";
import DraftCell from "@/components/chat/DraftCell.vue";
import { rowFormLayout } from "@/lib/rowForm";

const props = defineProps({
	table: { type: Object, required: true },
	row: { type: Object, required: true },
	// The record's own values, for rules that read `parent`.
	parent: { type: Object, default: () => ({}) },
	// Link-search keys are "t:{table index}:{row index}:{fieldname}".
	tableIndex: { type: Number, required: true },
	rowIndex: { type: Number, required: true },
	// Fields that differ from the saved row (an update), shown as changed.
	changedFields: { type: Object, default: () => new Set() },
	draftLink: { type: Object, required: true },
	palette: { type: Object, default: () => ({}) },
});
const emit = defineEmits(["link-search", "link-blur", "link-pick", "link-close"]);

const sections = computed(() =>
	rowFormLayout(props.table, props.row, props.parent, { index: props.rowIndex })
);
// The form's own link-search key: a grid column also shown here must not open a
// second suggestion menu over the same field (draftLinkSearch reads the first
// four parts, so the suffix changes nothing else).
const keyFor = (field) => `t:${props.tableIndex}:${props.rowIndex}:${field.fieldname}:form`;
const serverMissing = computed(() => new Set(props.row.__missing || []));
// Required and blank, or named by a failed save and still blank.
const isMissing = (item) =>
	!item.readOnly &&
	!String(props.row[item.field.fieldname] ?? "").trim() &&
	(item.reqd || serverMissing.value.has(item.field.fieldname));
const idFor = (field) => `jv-rowform-${props.tableIndex}-${props.rowIndex}-${field.fieldname}`;
</script>

<template>
	<div class="jv-rowform">
		<section v-for="(s, si) in sections" :key="si" class="jv-rowform-section">
			<div v-if="s.label" class="jv-rowform-section-title">{{ s.label }}</div>
			<div class="jv-rowform-cols">
				<div v-for="(col, ci) in s.columns" :key="ci" class="jv-rowform-col">
					<div
						v-for="item in col"
						:key="item.field.fieldname"
						class="jv-rowform-field"
						:class="{
							'jv-rowform-missing': isMissing(item),
							'jv-rowform-changed': changedFields.has(item.field.fieldname),
						}"
					>
						<label :for="idFor(item.field)" class="jv-rowform-label">
							{{ item.field.label
							}}<span v-if="item.reqd && !item.readOnly" class="jv-rowform-req">
								*</span
							>
						</label>
						<div class="jv-rowform-ctl">
							<DraftCell
								:field="item.field"
								:row="row"
								:cell-key="keyFor(item.field)"
								:read-only="item.readOnly"
								:control-id="idFor(item.field)"
								wide
								:draft-link="draftLink"
								:palette="palette"
								@link-search="(...a) => emit('link-search', ...a)"
								@link-blur="(e) => emit('link-blur', e)"
								@link-pick="(...a) => emit('link-pick', ...a)"
								@link-close="emit('link-close')"
							/>
						</div>
					</div>
				</div>
			</div>
		</section>
		<p v-if="!sections.length" class="jv-rowform-empty">No other fields apply to this row.</p>
	</div>
</template>

<style scoped>
/* The draft panel's own tokens (ChatView: --border, --surface-1, --text-2/3, --amber). */
.jv-rowform {
	display: grid;
	gap: 12px;
	padding: 10px 12px 12px;
	background: var(--surface-1);
	border-top: 1px solid var(--border);
}
.jv-rowform-section-title {
	font-size: 12px;
	font-weight: 650;
	color: var(--text-2);
	margin-bottom: 6px;
	overflow-wrap: anywhere;
}
.jv-rowform-cols {
	display: grid;
	grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr));
	gap: 10px 16px;
}
.jv-rowform-col {
	display: grid;
	gap: 10px;
	align-content: start;
	min-width: 0;
}
.jv-rowform-field {
	display: grid;
	gap: 4px;
	min-width: 0;
}
.jv-rowform-label {
	font-size: 12px;
	color: var(--text-3);
	/* A long label (or one long word) wraps instead of widening its column past a
	   phone screen. */
	overflow-wrap: anywhere;
}
.jv-rowform-req {
	color: var(--red);
}
.jv-rowform-missing :deep(.jv-action-input) {
	border-color: var(--amber-bd);
}
/* Differs from the saved row (an update), like a changed main field. */
.jv-rowform-changed .jv-rowform-label {
	color: var(--cta);
	font-weight: 600;
}
.jv-rowform-changed :deep(.jv-action-input),
.jv-rowform-changed :deep(.jv-cell-ro) {
	box-shadow: inset 3px 0 0 var(--cta);
}
.jv-rowform-ctl {
	min-width: 0;
}
.jv-rowform-ctl :deep(.jv-action-input) {
	width: 100%;
	max-width: 100%;
	min-width: 0;
	box-sizing: border-box;
}
.jv-rowform-ctl :deep(.jv-cell-ro) {
	overflow-wrap: anywhere;
}
.jv-rowform-ctl :deep(.jv-cell-ro) {
	color: var(--text-3);
}
.jv-rowform-empty {
	margin: 0;
	font-size: 12px;
	color: var(--text-3);
}
</style>
