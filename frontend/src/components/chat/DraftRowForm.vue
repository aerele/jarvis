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
		<!-- A collapsible section (Desk's Section Break "collapsible") is a details
		     element, its heading the toggle; it starts open when it holds what the user
		     must see (rowFormLayout). Any other section is a plain titled group. -->
		<component
			:is="s.collapsible ? 'details' : 'section'"
			v-for="s in sections"
			:key="s.id"
			class="jv-rowform-section"
			:open="s.collapsible ? s.open : undefined"
			:aria-label="s.collapsible ? undefined : s.label || undefined"
		>
			<component
				:is="s.collapsible ? 'summary' : 'div'"
				v-if="s.label"
				class="jv-rowform-section-title"
				>{{ s.label }}</component
			>
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
		</component>
		<p v-for="n in needed" :key="n" class="jv-rowform-needed" role="note">
			Also needed: {{ n }}
		</p>
		<p v-if="!sections.length && !needed.length" class="jv-rowform-empty">
			No other fields apply to this row.
		</p>
	</div>
</template>

<style scoped>
/* The draft panel's own patterns and tokens (ChatView: .jv-draft-fld labels, the
   table title, --border, --surface, --text-2/3, --cta, --amber), so a row reads like
   the record's main fields above it. */
.jv-rowform {
	display: grid;
	gap: 12px;
	padding: 12px 14px 14px;
	background: var(--surface);
	border-top: 1px solid var(--border);
}
/* Sections are divided by a rule, as Desk divides them. */
.jv-rowform-section + .jv-rowform-section {
	border-top: 1px solid var(--border);
	padding-top: 12px;
}
.jv-rowform-section-title {
	font-size: 12px;
	font-weight: 650;
	color: var(--text-2);
	margin-bottom: 8px;
	overflow-wrap: anywhere;
}
summary.jv-rowform-section-title {
	display: flex;
	align-items: center;
	gap: 6px;
	cursor: pointer;
	list-style: none;
	min-height: 28px;
	margin-bottom: 0;
}
summary.jv-rowform-section-title::-webkit-details-marker {
	display: none;
}
summary.jv-rowform-section-title::before {
	content: "▸";
	font-size: 10px;
	color: var(--text-3);
	transition: transform 0.12s;
}
details[open] > summary.jv-rowform-section-title {
	margin-bottom: 8px;
}
details[open] > summary.jv-rowform-section-title::before {
	transform: rotate(90deg);
}
summary.jv-rowform-section-title:hover {
	color: var(--text);
}
summary.jv-rowform-section-title:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 2px;
	border-radius: 4px;
}
.jv-rowform-cols {
	display: grid;
	grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr));
	gap: 10px 12px;
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
/* As a main field's label (.jv-draft-fld label). */
.jv-rowform-label {
	font-size: 10.5px;
	font-weight: 650;
	letter-spacing: 0.06em;
	text-transform: uppercase;
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
/* Differs from the saved row (an update): tinted as a changed main field is
   (.jv-draft-fld.changed), and its label in the accent. */
.jv-rowform-changed .jv-rowform-label {
	color: var(--cta);
}
.jv-rowform-changed :deep(.jv-action-input) {
	border-color: var(--cta-bd);
	background: var(--cta-bg);
}
.jv-rowform-changed :deep(.jv-cell-ro) {
	box-shadow: inset 3px 0 0 var(--cta);
	padding-left: 8px;
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
/* A read-only value, as a main one (.jv-draft-ro). */
.jv-rowform-ctl :deep(.jv-cell-ro) {
	display: inline-block;
	padding: 4px 0;
	color: var(--text-2);
	overflow-wrap: anywhere;
}
.jv-rowform-empty {
	margin: 0;
	font-size: 12px;
	color: var(--text-3);
}
</style>
