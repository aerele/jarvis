<script setup>
// A child row's full form in the record draft panel: every field Desk's row edit
// form shows for this row, in its sections and columns, with depends_on and its
// siblings evaluated as the row changes (lib/rowForm.js). The grid shows a few
// columns; this shows the rest, so nothing the record would save is out of reach.
import { computed } from "vue";
import DraftCell from "@/components/chat/DraftCell.vue";
import { allRowFields, toPanelRow } from "@/lib/draftApply";
import { changeDisplay, clipText, rowFormLayout } from "@/lib/rowForm";

const props = defineProps({
	table: { type: Object, required: true },
	row: { type: Object, required: true },
	// The record's own values, for rules that read `parent`.
	parent: { type: Object, default: () => ({}) },
	// Link-search keys are "t:{table index}:{row.__key}:{fieldname}": the row's stable
	// key, not its position (a row removed above would move it). `rowIndex` is only the
	// row's place, for the rules' `doc.idx`.
	tableIndex: { type: Number, required: true },
	rowIndex: { type: Number, required: true },
	// Fields that differ from the saved row (an update), shown as changed, and the saved
	// row itself (null for a new row): a changed field says what it was, and a field a
	// rule hides stays hidden while unchanged.
	changedFields: { type: Object, default: () => new Set() },
	savedRow: { type: Object, default: null },
	draftLink: { type: Object, required: true },
	palette: { type: Object, default: () => ({}) },
});
const emit = defineEmits(["link-search", "link-blur", "link-pick", "link-close"]);

const sections = computed(() =>
	rowFormLayout(props.table, props.row, props.parent, {
		index: props.rowIndex,
		changed: props.savedRow ? props.changedFields : null,
	})
);
const before = computed(() =>
	props.savedRow ? toPanelRow(allRowFields(props.table), props.savedRow) : null
);
// What a changed field was, worded as the preview words it. The text can be long (a
// saved description), so the shown text is clipped (clipText) and the title has it all.
const wasFull = (item) =>
	before.value && props.changedFields.has(item.field.fieldname)
		? `was ${changeDisplay(item.field, before.value[item.field.fieldname])}`
		: "";
const wasText = (item) => clipText(wasFull(item));
// The form's own link-search key: a grid column also shown here must not open a
// second suggestion menu over the same field (draftLinkSearch reads the first
// four parts, so the suffix changes nothing else).
const keyFor = (field) => `t:${props.tableIndex}:${props.row.__key}:${field.fieldname}:form`;
const serverMissing = computed(() => new Set(props.row.__missing || []));
const isBlank = (name) => !String(props.row[name] ?? "").trim();
// DraftCell shows these as a static value, not a control (so no <label for> fits).
const isStatic = (item) => item.readOnly || item.field.fieldtype === "Read Only";
// Named by a failed save and still blank (rowStatus explains why a required blank
// alone is not flagged; its label keeps the star). A static value has no control to
// fill: it is listed under "Also needed" instead (below).
const isMissing = (item) =>
	!isStatic(item) &&
	isBlank(item.field.fieldname) &&
	serverMissing.value.has(item.field.fieldname);
const idFor = (field) => `jv-rowform-${props.tableIndex}-${props.row.__key}-${field.fieldname}`;
// The visible label's id, which names a read-only value (it has no <label for>).
const labelIdFor = (field) => `${idFor(field)}-label`;
// The ids of the "Required" and "was X" texts, which describe the control beside them.
const reqIdFor = (field) => `${idFor(field)}-req`;
const wasIdFor = (field) => `${idFor(field)}-was`;
const describedBy = (item) =>
	[isMissing(item) && reqIdFor(item.field), wasText(item) && wasIdFor(item.field)]
		.filter(Boolean)
		.join(" ");
const isRequired = (item) =>
	!isStatic(item) && (!!item.reqd || serverMissing.value.has(item.field.fieldname));
// Names a failed save asked for that the user cannot fill here and that are still blank:
// a name the form has no field for (hidden, unreadable, or not in the form meta), or one
// it shows read-only. Each is listed once.
const needed = computed(() => {
	const fillable = new Set(
		sections.value
			.flatMap((s) => s.columns.flat())
			.filter((i) => !isStatic(i))
			.map((i) => i.field.fieldname)
	);
	const labels = new Map(allRowFields(props.table).map((f) => [f.fieldname, f.label]));
	const names = (props.row.__missing || []).filter((n) => !fillable.has(n) && isBlank(n));
	return [...new Set(names)].map((n) => labels.get(n) || n);
});
</script>

<template>
	<div class="jv-rowform">
		<section v-for="s in sections" :key="s.id" class="jv-rowform-section">
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
						<label
							:id="labelIdFor(item.field)"
							:for="isStatic(item) ? undefined : idFor(item.field)"
							class="jv-rowform-label"
						>
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
								:labelledby="labelIdFor(item.field)"
								:describedby="describedBy(item)"
								:required="isRequired(item)"
								wide
								:draft-link="draftLink"
								:palette="palette"
								@link-search="(...a) => emit('link-search', ...a)"
								@link-blur="(e) => emit('link-blur', e)"
								@link-pick="(...a) => emit('link-pick', ...a)"
								@link-close="emit('link-close')"
							/>
							<div
								v-if="wasText(item)"
								:id="wasIdFor(item.field)"
								class="jv-rowform-was"
								:title="wasFull(item)"
							>
								{{ wasText(item) }}
							</div>
							<div
								v-if="isMissing(item)"
								:id="reqIdFor(item.field)"
								class="jv-rowform-reqtext"
							>
								Required
							</div>
						</div>
					</div>
				</div>
			</div>
		</section>
		<p v-for="n in needed" :key="n" class="jv-rowform-needed" role="note">
			Also needed: {{ n }}
		</p>
		<p v-if="!sections.length && !needed.length" class="jv-rowform-empty">
			No other fields apply to this row.
		</p>
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
.jv-rowform-was {
	margin-top: 3px;
	font-size: 11.5px;
	color: var(--text-3);
	overflow-wrap: anywhere;
}
/* Words as well as the amber border, so a blank required field is not colour only. */
.jv-rowform-reqtext,
.jv-rowform-needed {
	margin: 3px 0 0;
	font-size: 11.5px;
	font-weight: 600;
	color: var(--amber);
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
