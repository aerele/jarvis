<template>
	<div ref="root" class="jv-draft-body" :class="{ 'jv-surface-phone': surface === 'phone' }">
		<slot name="top" />
		<p v-if="linkError" class="jv-draft-toast" role="alert">
			{{ linkError }}
		</p>
		<div v-if="model.updatedToast" class="jv-draft-toast">Draft updated from chat</div>
		<div class="jv-draft-fields">
			<div
				v-for="f in model.fields"
				:key="f.fieldname"
				class="jv-draft-fld"
				:class="{
					missing: isFieldMissing(f),
					invalid: f.serverInvalid,
					changed: f.changed,
				}"
			>
				<label
					>{{ f.label
					}}<span v-if="(f.reqd || f.serverMissing) && !f.read_only" class="jv-req">
						*</span
					></label
				>
				<div class="jv-draft-ctl">
					<span v-if="f.read_only" class="jv-draft-ro">{{ readonlyDisplay(f) }}</span>
					<template v-else-if="f.control === 'link'">
						<input
							class="jv-action-input"
							v-model="f.value"
							@input="
								onDraftLink('f:' + f.fieldname, () => f.value, f.options, $event)
							"
							@focus="
								onDraftLink('f:' + f.fieldname, () => f.value, f.options, $event)
							"
							@blur="closeDraftLink"
							:placeholder="'Search ' + (f.options || 'records') + '…'"
							autocomplete="off"
						/>
						<DraftLinkMenu
							v-if="
								draftLink.open &&
								draftLink.key === 'f:' + f.fieldname &&
								draftLink.items.length
							"
							:anchor="draftLink.anchor"
							:items="draftLink.items"
							:palette="palette"
							@pick="
								(it) =>
									pickDraftLink((v) => {
										f.value = v;
									}, it)
							"
							@close="emit('link-close')"
						/>
					</template>
					<select
						v-else-if="f.control === 'select'"
						class="jv-action-input jv-action-sel"
						v-model="f.value"
					>
						<option v-for="o in f.options" :key="o" :value="o">
							{{ o }}
						</option>
					</select>
					<select
						v-else-if="f.control === 'check'"
						class="jv-action-input jv-action-sel"
						v-model="f.value"
					>
						<option>Yes</option>
						<option>No</option>
					</select>
					<input
						v-else-if="f.control === 'date'"
						type="date"
						class="jv-action-input"
						v-model="f.value"
					/>
					<input
						v-else-if="f.control === 'datetime'"
						type="datetime-local"
						class="jv-action-input"
						v-model="f.value"
					/>
					<input
						v-else-if="f.control === 'time'"
						type="time"
						class="jv-action-input"
						v-model="f.value"
					/>
					<input
						v-else-if="f.control === 'number'"
						type="number"
						class="jv-action-input"
						v-model="f.value"
					/>
					<textarea
						v-else-if="f.control === 'text'"
						class="jv-action-input"
						v-model="f.value"
						rows="3"
					></textarea>
					<input v-else class="jv-action-input" v-model="f.value" />
				</div>
			</div>
		</div>
		<div v-for="(t, ti) in model.tables" :key="t.fieldname" class="jv-draft-table">
			<div class="jv-draft-table-title">{{ t.label }}</div>
			<div v-if="removedSavedRows(t).length" class="jv-rows-removed" role="note">
				Saving removes {{ removedSavedRows(t).length }} saved
				{{ removedSavedRows(t).length === 1 ? "row" : "rows" }}:
				{{ removedSavedRows(t).join(", ") }}
			</div>
			<div v-if="draftChangeSummaries[ti]" class="jv-rows-changed" role="note">
				<template v-if="draftChangeSummaries[ti].grouped">
					<span>{{ draftChangeSummaries[ti].text }}</span>
					<button
						type="button"
						class="jv-rows-changed-open"
						@click="openChangedRows(t, draftChangeSummaries[ti])"
					>
						{{ draftChangeSummaries[ti].openLabel }}
					</button>
				</template>
				<template v-else>
					<span>{{ draftChangeSummaries[ti].lead }}</span>
					<button
						v-for="i in draftChangeSummaries[ti].rows"
						:key="i"
						type="button"
						class="jv-rows-changed-open"
						:aria-label="'Open row ' + (i + 1)"
						@click="showRowForm(ti, t.rows[i])"
					>
						{{ i + 1 }}
					</button>
				</template>
			</div>
			<div class="jv-draft-gridwrap">
				<table class="jv-grid">
					<thead>
						<tr>
							<th class="jv-grid-lead">
								<span class="jv-sr">Row</span>
							</th>
							<th v-for="c in t.columns" :key="c.fieldname">
								{{ c.label }}
							</th>
							<th class="jv-grid-x"></th>
						</tr>
					</thead>
					<tbody>
						<template v-for="(r, ri) in t.rows" :key="r.__key">
							<!-- An open row is its full form in place, as Desk opens a
							     row: a card the panel's width at the row's own spot. -->
							<tr
								v-if="r.__open"
								class="jv-grid-openrow"
								:class="{ 'jv-grid-flagged': r.__flag }"
								@input="clearRowFlag(r)"
								@change="clearRowFlag(r)"
							>
								<td :colspan="t.columns.length + 2" class="jv-grid-opencell">
									<DraftRowBlock
										:table="t"
										:row="r"
										:parent="rowMemo.parentFor(t)"
										:table-index="ti"
										:row-index="ri"
										:changed-fields="rowStatusFor(t, r, ri).changedFields"
										:saved-row="rowStatusFor(t, r, ri).savedRow"
										:draft-link="draftLink"
										:palette="palette"
										@close="closeRowForm(ti, r)"
										@remove="removeDraftRow(ti, ri)"
										@insert="insertDraftRowBelow(ti, ri)"
										@link-search="onDraftLink"
										@link-blur="closeDraftLink"
										@link-pick="pickDraftLink"
										@link-close="emit('link-close')"
									/>
								</td>
							</tr>
							<tr v-else>
								<td class="jv-grid-lead">
									<div class="jv-grid-lead-in">
										<span class="jv-grid-num">{{ ri + 1 }}</span>
										<button
											v-if="t.formFields && t.formFields.length"
											type="button"
											:id="rowToggleId(ti, r.__key)"
											class="jv-grid-more"
											:class="{
												'jv-grid-more-warn': rowStatusFor(t, r, ri)
													.missing,
											}"
											:aria-expanded="r.__open ? 'true' : 'false'"
											:aria-controls="
												r.__open ? rowFormId(ti, r.__key) : undefined
											"
											:title="rowToggleLabel(t, r, ri)"
											:aria-label="rowToggleLabel(t, r, ri)"
											@click="toggleRowForm(ti, r)"
										>
											<!-- Desk's row edit icon: opens the row's full form. -->
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
												<path
													d="M21.17 6.81a1 1 0 0 0-3.99-3.98L3.84 16.17a2 2 0 0 0-.5.83l-1.32 4.35a.5.5 0 0 0 .62.62l4.35-1.32a2 2 0 0 0 .83-.5z"
												/>
												<path d="m15 5 4 4" />
											</svg>
											<span
												v-for="part in toggleParts(rowStatusFor(t, r, ri))"
												:key="part.kind"
												class="jv-grid-badge"
												:class="{
													'jv-grid-badge-warn': part.kind === 'missing',
												}"
												aria-hidden="true"
												>{{ part.text }}</span
											>
										</button>
									</div>
								</td>
								<td
									v-for="c in t.columns"
									:key="c.fieldname"
									:class="{ 'jv-grid-ro': c.read_only }"
								>
									<DraftCell
										:field="c"
										:row="r"
										:cell-key="'t:' + ti + ':' + r.__key + ':' + c.fieldname"
										:read-only="!!c.read_only"
										:label="c.label"
										:draft-link="draftLink"
										:palette="palette"
										@link-search="onDraftLink"
										@link-blur="closeDraftLink"
										@link-pick="pickDraftLink"
										@link-close="emit('link-close')"
									/>
								</td>
								<td class="jv-grid-x">
									<span class="jv-grid-acts">
										<button
											type="button"
											class="jv-grid-del"
											@click="removeDraftRow(ti, ri)"
											title="Remove row"
											:aria-label="'Remove row ' + (ri + 1)"
										>
											✕
										</button>
									</span>
								</td>
							</tr>
						</template>
					</tbody>
				</table>
			</div>
			<button class="jv-draft-addrow" @click="addDraftRow(ti)">＋ Add row</button>
		</div>
		<div v-if="draftTotals" class="jv-draft-totals">
			<div class="jv-draft-total-fld">
				<label>Total Qty</label>
				<div class="jv-draft-total-ctl">
					<input
						class="jv-action-input jv-draft-total-input"
						:value="draftTotals.qty"
						readonly
						tabindex="-1"
						aria-readonly="true"
					/>
					<svg
						class="jv-draft-total-lock"
						width="12"
						height="12"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.9"
						stroke-linecap="round"
						stroke-linejoin="round"
						aria-hidden="true"
					>
						<rect x="3" y="11" width="18" height="11" rx="2" />
						<path d="M7 11V7a5 5 0 0 1 10 0v4" />
					</svg>
				</div>
			</div>
			<div v-if="draftTotals.hasAmt" class="jv-draft-total-fld">
				<label>Est. Total</label>
				<div class="jv-draft-total-ctl">
					<input
						class="jv-action-input jv-draft-total-input"
						:value="draftTotals.amount"
						readonly
						tabindex="-1"
						aria-readonly="true"
					/>
					<svg
						class="jv-draft-total-lock"
						width="12"
						height="12"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.9"
						stroke-linecap="round"
						stroke-linejoin="round"
						aria-hidden="true"
					>
						<rect x="3" y="11" width="18" height="11" rx="2" />
						<path d="M7 11V7a5 5 0 0 1 10 0v4" />
					</svg>
				</div>
			</div>
			<p class="jv-draft-est">Auto-calculated · ERPNext computes the final totals.</p>
		</div>
		<div v-if="model.error" style="margin-top: 10px">
			<slot name="error" />
		</div>
		<!-- Always present, so a screen reader announces the note when it fills. -->
		<div
			ref="parkedRegion"
			class="jv-draft-parked"
			:class="{ 'is-empty': !model.parked }"
			role="status"
			aria-live="polite"
			tabindex="-1"
			style="margin-top: 10px"
		>
			{{ model.parked || "" }}
		</div>
	</div>
</template>

<script setup>
// The draft edit form's BODY: the main fields, the child-table grids, the totals row and the
// parked-note region. The panel (desktop side panel, phone sheet) owns the shell around it
// (overlay, header, footer buttons) and the save. This component edits the `model` it is
// given IN PLACE, exactly as the panel's template did before it moved here. The link search
// is a state machine the host owns: it is driven through the `link-*` events (the same
// signatures DraftCell and DraftRowBlock emit) and shown from the read-only `draftLink`.
// Relative imports only (see draftFormImportGraph.spec.js): the phone resolves `@/` elsewhere.
import { computed, onMounted, onUpdated, provide, ref, toRef } from "vue";
import DraftCell from "./DraftCell.vue";
import DraftLinkMenu from "./DraftLinkMenu.vue";
import DraftRowBlock from "./DraftRowBlock.vue";
import { isFieldMissing, readonlyDisplay, removedSavedRows } from "../../lib/draftApply";
import { blankRow, openChangedRows, toggleLabel, toggleParts } from "../../lib/draftTables";
import { draftTotals as totalsOf } from "../../lib/draftTotals";
import { DRAFT_SURFACE } from "../../lib/draftSurface";
import { createRowStatusMemo } from "../../lib/rowStatusMemo";
import {
	closeRowForm,
	rowFormId,
	rowToggleId,
	showRowForm,
	toggleRowForm,
} from "../../lib/rowFormFocus";

const props = defineProps({
	// The draft model the panel edits (lib/draftSave stampDraftModel): fields, tables, error, parked, ...
	model: { type: Object, required: true },
	// The host's link-search state { key, items, open, anchor }: read here, never assigned.
	draftLink: { type: Object, required: true },
	// The theme's CSS variables, for the teleported link menu (it renders outside the host's scope).
	palette: { type: Object, default: () => ({}) },
	// A link-search failure the host wants shown above the fields.
	linkError: { type: String, default: "" },
	// "desktop" (the default: exactly what the desktop panel has always drawn) or "phone" (16px
	// inputs, 40px targets, one column, table rows as stacked cards; see the end of the style).
	surface: { type: String, default: "desktop" },
});
// Slot `top` (the phone's host: its notices, at the top of the scrolling body) draws nothing when
// empty. The link menu is teleported to <body>, so it cannot see this component's classes: it asks.
provide(DRAFT_SURFACE, toRef(props, "surface"));
const emit = defineEmits(["link-search", "link-blur", "link-pick", "link-close"]);
// The host answers these; the arguments pass through unchanged.
const onDraftLink = (...a) => emit("link-search", ...a);
// The row's "Row N:" mark (lib/draftRowError.js) goes when the person edits that row; the next save reply
// clears it too (markRowFromMessage resets every row's flag).
const clearRowFlag = (row) => {
	if (row.__flag) delete row.__flag;
};
const closeDraftLink = (e) => emit("link-blur", e);
const pickDraftLink = (...a) => emit("link-pick", ...a);

// A row's form status for its toggle and form: blank required fields, and (on an
// update) what differs from the saved row. One computed per row that keeps its
// object while nothing in the status changed, and one parent object per table with
// just the record values its rules read (lib/rowStatusMemo.js): typing in a row, or
// in a main field no rule reads, does not re-render the other rows.
const rowMemo = createRowStatusMemo(() => props.model);
// Above each grid, the rows the agent (or the user) changed outside it: named when
// few, grouped by field when many, with a button that opens them (draftTables).
const draftChangeSummaries = rowMemo.summaries;
const rowStatusFor = rowMemo.rowStatusFor;
// The toggle's accessible name: which row, and what needs attention in it.
function rowToggleLabel(t, r, ri) {
	return toggleLabel(ri + 1, rowStatusFor(t, r, ri));
}
// est. totals: any grid with qty (+rate) columns
const draftTotals = computed(() => totalsOf(props.model));

function addDraftRow(ti) {
	const t = props.model.tables[ti];
	t.rows.push(blankRow(t));
}
function removeDraftRow(ti, ri) {
	props.model.tables[ti].rows.splice(ri, 1);
}
// Insert below, from an open row's card (as Desk's row editor): a blank row right
// after it, opened in its place while the row it came from closes.
function insertDraftRowBelow(ti, ri) {
	const t = props.model.tables[ti];
	t.rows[ri].__open = false;
	t.rows.splice(ri + 1, 0, blankRow(t));
	showRowForm(ti, t.rows[ri + 1]);
}

// On the phone a table row is a card and each cell sits under its column's name. The name is the
// cell's `data-label` (the stylesheet prints it); it is put on the cells here, after each render,
// and not in the grid's template, so the desktop's markup is exactly what it was.
const root = ref(null);
function labelPhoneCells() {
	if (props.surface !== "phone" || !root.value) return;
	const grids = root.value.querySelectorAll(".jv-grid");
	(props.model.tables || []).forEach((t, ti) => {
		const body = grids[ti] && grids[ti].tBodies[0];
		for (const tr of body ? body.rows : []) {
			// the first cell is the row's number and toggle, the last its remove button
			if (tr.classList.contains("jv-grid-openrow")) continue;
			(t.columns || []).forEach((c, i) => {
				if (tr.cells[i + 1]) tr.cells[i + 1].setAttribute("data-label", c.label);
			});
		}
	});
}
onMounted(labelPhoneCells);
onUpdated(labelPhoneCells);

// Always-present status region (focused by the host after a save is parked as a confirmation).
const parkedRegion = ref(null);
defineExpose({ parkedRegion });
</script>

<style scoped>
/* Shared with ChatView's other cards, so COPIED here (ChatView keeps its own): the
   form's inputs and the visually-hidden header label. Same declarations, same order. */
/* visually-hidden live region for screen-reader announcements (UX #5) */
.jv-sr {
	position: absolute;
	width: 1px;
	height: 1px;
	margin: -1px;
	padding: 0;
	border: 0;
	overflow: hidden;
	clip: rect(0 0 0 0);
	clip-path: inset(50%);
	white-space: nowrap;
}
.jv-action-input {
	width: 100%;
	box-sizing: border-box;
	font-family: inherit;
	font-size: 13px;
	line-height: 1.45;
	color: var(--text);
	background: var(--surface);
	border: 1px solid var(--border-2);
	border-radius: 7px;
	padding: 6px 9px;
	outline: none;
	resize: vertical;
	transition: border-color 0.12s, box-shadow 0.12s;
}
.jv-action-input:focus {
	border-color: var(--cta);
	box-shadow: 0 0 0 3px var(--cta-bg);
}
.jv-action-sel {
	cursor: pointer;
	appearance: auto;
}
.jv-draft-body {
	flex: 1;
	overflow-y: auto;
	padding: 14px 16px;
	display: flex;
	flex-direction: column;
	gap: 14px;
}
.jv-draft-toast {
	font-size: 12px;
	color: var(--cta);
	background: var(--cta-bg);
	border: 1px solid var(--cta-bd);
	border-radius: 8px;
	padding: 7px 11px;
}
.jv-draft-fields {
	display: grid;
	grid-template-columns: 1fr 1fr;
	gap: 10px 12px;
}
.jv-draft-fld label {
	display: block;
	font-size: 10.5px;
	font-weight: 650;
	letter-spacing: 0.06em;
	text-transform: uppercase;
	color: var(--text-3);
	margin-bottom: 4px;
}
.jv-draft-fld .jv-req {
	color: var(--red);
}
.jv-draft-fld.missing .jv-action-input {
	border-color: var(--amber-bd);
}
/* a value the save refused (a bad option or link): marked until the next save */
.jv-draft-fld.invalid .jv-action-input {
	border-color: var(--red);
}
.jv-draft-fld.changed .jv-action-input {
	border-color: var(--cta-bd);
	background: var(--cta-bg);
}
.jv-draft-ctl {
	position: relative;
}
/* A read_only main field is shown, not editable: a muted static value (mirrors the
   child-grid jv-grid-ro treatment) so the user can't type into a field the confirm
   would discard. A proposed read_only value still shows here for review. */
.jv-draft-ro {
	display: inline-block;
	padding: 4px 0;
	color: var(--text-2);
	word-break: break-word;
}
.jv-rows-changed {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 6px 10px;
	padding: 6px 10px;
	margin-bottom: 6px;
	border-radius: 6px;
	border: 1px solid var(--cta-bd);
	background: var(--cta-bg);
	color: var(--cta);
	font-size: 12.5px;
	overflow-wrap: anywhere;
}
.jv-rows-changed-open {
	background: none;
	border: 1px solid var(--cta-bd);
	border-radius: 7px;
	color: var(--cta);
	font-weight: 600;
	font-size: 12.5px;
	cursor: pointer;
	padding: 2px 10px;
	min-height: 32px;
}
.jv-rows-changed-open:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 1px;
}
.jv-rows-removed {
	padding: 6px 10px;
	margin-bottom: 6px;
	border-radius: 6px;
	border: 1px solid var(--red-bd);
	background: var(--red-bg);
	color: var(--red);
	font-size: 12px;
}
.jv-draft-table-title {
	font-size: 12px;
	font-weight: 650;
	color: var(--text-2);
	margin-bottom: 6px;
}
.jv-draft-gridwrap {
	overflow-x: auto;
	border: 1px solid var(--border);
	border-radius: 9px;
}
.jv-grid {
	width: 100%;
	border-collapse: collapse;
	font-size: 13px;
}
.jv-grid th {
	font-size: 10.5px;
	font-weight: 650;
	letter-spacing: 0.05em;
	text-transform: uppercase;
	color: var(--text-3);
	text-align: left;
	padding: 7px 8px;
	border-bottom: 1px solid var(--border-2);
	background: var(--surface-1);
}
.jv-grid td {
	padding: 5px 6px;
	border-bottom: 1px solid var(--border);
	position: relative;
	min-width: 90px;
}
/* An open row (DraftRowBlock in place): one cell across the whole row, its card pinned
   to the left at the scroller's visible width (the gridwrap is an inline-size
   container), so it fits the panel however far the grid is scrolled sideways. */
.jv-draft-gridwrap {
	container-type: inline-size;
}
.jv-grid td.jv-grid-opencell {
	padding: 0;
	min-width: 0;
	background: var(--surface);
}
.jv-grid-opencell > :deep(.jv-rowform-block) {
	position: sticky;
	left: 0;
	width: 100cqi;
	box-sizing: border-box;
}
.jv-grid td .jv-action-input {
	width: 100%;
	box-sizing: border-box;
}
.jv-grid-ro {
	color: var(--text-3);
}
.jv-grid-x {
	width: 1%;
	white-space: nowrap;
	min-width: 0 !important;
}
/* The leading cell: row number + the row-form toggle, first in the row so they are on
   screen at scrollLeft 0 on a phone without sticky CSS. At most 96px; the extra
   specificity beats `.jv-grid td { min-width: 90px }`. The badges wrap under the arrow
   rather than widening it. */
.jv-grid td.jv-grid-lead,
.jv-grid th.jv-grid-lead {
	width: 96px;
	max-width: 96px;
	min-width: 0;
	box-sizing: border-box;
}
.jv-grid th.jv-grid-lead {
	position: relative; /* anchors the visually-hidden "Row" label */
}
/* Inside the cell, not on it: a flex td would leave table layout. */
.jv-grid-lead-in {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 4px;
}
.jv-grid-num {
	min-width: 1.4em;
	font-size: 11.5px;
	font-variant-numeric: tabular-nums;
	color: var(--text-3);
}
.jv-grid-lead .jv-grid-more {
	flex-wrap: wrap;
	max-width: 100%;
	box-sizing: border-box;
}
.jv-grid-acts {
	display: inline-flex;
	align-items: center;
	gap: 6px;
}
/* Opens a row's full form (DraftRowForm, below the grid). A ~32px target, kept
   apart from Remove so a thumb aimed at one does not hit the other. */
.jv-grid-more,
.jv-grid-del {
	min-width: 32px;
	min-height: 32px;
	display: inline-flex;
	align-items: center;
	justify-content: center;
	gap: 3px;
}
.jv-grid-more {
	background: none;
	border: 1px solid var(--border);
	color: var(--text-3);
	cursor: pointer;
	padding: 2px 6px;
	border-radius: 7px;
}
.jv-grid-more:hover,
.jv-grid-more[aria-expanded="true"] {
	color: var(--cta);
	background: var(--surface-1);
}
.jv-grid-more-warn {
	border-color: var(--amber-bd);
}
.jv-grid-badge {
	font-size: 10.5px;
	font-weight: 650;
	line-height: 16px;
	min-width: 16px;
	padding: 0 4px;
	border-radius: 8px;
	background: var(--surface-1);
	color: var(--text-2);
}
.jv-grid-badge-warn {
	background: var(--amber-bg);
	color: var(--amber);
}
.jv-grid-more:focus-visible,
.jv-grid-del:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 1px;
}
.jv-grid-del {
	background: none;
	border: none;
	color: var(--text-3);
	cursor: pointer;
	padding: 4px 6px;
	border-radius: 6px;
}
.jv-grid-del:hover {
	color: var(--red);
	background: var(--red-bg);
}
.jv-draft-addrow {
	align-self: flex-start;
	margin-top: 8px;
	background: none;
	border: none;
	color: var(--cta);
	font-weight: 600;
	font-size: 12.5px;
	cursor: pointer;
	padding: 4px 2px;
}
.jv-draft-totals {
	display: flex;
	flex-wrap: wrap;
	align-items: flex-end;
	gap: 10px 14px;
	padding: 12px 14px;
	border: 1px solid var(--border);
	border-radius: 10px;
	background: var(--surface-1);
}
.jv-draft-total-fld {
	display: flex;
	flex-direction: column;
	flex: 1 1 130px;
	min-width: 120px;
}
.jv-draft-total-fld label {
	display: block;
	font-size: 10.5px;
	font-weight: 650;
	letter-spacing: 0.06em;
	text-transform: uppercase;
	color: var(--text-3);
	margin-bottom: 4px;
}
.jv-draft-total-ctl {
	position: relative;
}
/* read-only: muted fill, no caret, a lock glyph — clearly not an editable field */
.jv-draft-total-input {
	width: 100%;
	box-sizing: border-box;
	background: var(--surface-2);
	color: var(--text);
	font-weight: 650;
	font-variant-numeric: tabular-nums;
	cursor: default;
	padding-right: 28px;
}
.jv-draft-total-input:focus {
	border-color: var(--border-2);
	box-shadow: none;
}
.jv-draft-total-lock {
	position: absolute;
	right: 9px;
	top: 50%;
	transform: translateY(-50%);
	color: var(--text-3);
	pointer-events: none;
}
.jv-draft-parked {
	padding: 10px 12px;
	border: 1px solid var(--border);
	border-radius: 8px;
	color: var(--text-2);
	font-size: 13px;
}
.jv-draft-parked.is-empty {
	padding: 0;
	border: 0;
	margin-top: 0 !important;
}
.jv-draft-est {
	flex-basis: 100%;
	margin: 0;
	color: var(--text-3);
	font-size: 11px;
}
@media (max-width: 700px) {
	.jv-draft-fields {
		grid-template-columns: 1fr;
	}
}
/* ---- surface="phone" -------------------------------------------------------------------
   Appended after every desktop rule (draftSurface.spec.js holds the old text as a prefix) and all
   behind `.jv-surface-phone`, so the desktop cannot change. Inputs are 16px (iOS zooms the page
   on focus below that) and every control and row button is at least 40px tall. Fields are one
   column. A table is not a scrolling grid: each row is a card, every cell under its column name
   (the cell's `data-label`), the row's edit toggle (the existing stacked row editor,
   DraftRowBlock) beside the row number and its remove button in the corner. The body scrolls
   inside the sheet and never sideways. */
.jv-surface-phone {
	overflow-x: hidden;
	overscroll-behavior: contain;
	-webkit-overflow-scrolling: touch;
	padding-bottom: 28px;
}
.jv-surface-phone .jv-action-input,
.jv-surface-phone :deep(.jv-action-input) {
	font-size: 16px;
	line-height: 1.35;
	min-height: 40px;
	padding: 8px 10px;
}
.jv-surface-phone .jv-draft-fields {
	grid-template-columns: 1fr;
}
.jv-surface-phone .jv-draft-fld label,
.jv-surface-phone .jv-draft-total-fld label {
	font-size: 11.5px;
}
.jv-surface-phone .jv-draft-ro {
	font-size: 15px;
	padding: 8px 0;
}
.jv-surface-phone .jv-draft-table-title {
	font-size: 13px;
}
.jv-surface-phone .jv-draft-gridwrap {
	border: 0;
	border-radius: 0;
}
.jv-surface-phone .jv-grid,
.jv-surface-phone .jv-grid tbody,
.jv-surface-phone .jv-grid tr,
.jv-surface-phone .jv-grid td {
	display: block;
	width: auto;
	box-sizing: border-box;
}
.jv-surface-phone .jv-grid thead {
	display: none;
}
.jv-surface-phone .jv-grid tr {
	position: relative;
	margin-bottom: 10px;
	padding: 8px 10px 10px;
	border: 1px solid var(--border);
	border-radius: 10px;
	background: var(--surface);
}
.jv-surface-phone .jv-grid tr.jv-grid-openrow {
	padding: 0;
	border: 0;
	background: none;
}
.jv-surface-phone .jv-grid td {
	min-width: 0;
	padding: 6px 0 0;
	border: 0;
}
.jv-surface-phone .jv-grid td[data-label]::before {
	content: attr(data-label);
	display: block;
	margin-bottom: 3px;
	font-size: 11.5px;
	font-weight: 650;
	letter-spacing: 0.04em;
	text-transform: uppercase;
	color: var(--text-3);
}
.jv-surface-phone .jv-grid td.jv-grid-opencell {
	padding: 0;
}
.jv-surface-phone .jv-grid td.jv-grid-lead {
	width: auto;
	max-width: none;
	padding: 0 48px 4px 0;
}
.jv-surface-phone .jv-grid td.jv-grid-x {
	position: absolute;
	top: 4px;
	right: 4px;
	width: auto;
	padding: 0;
}
.jv-surface-phone .jv-grid-more,
.jv-surface-phone .jv-grid-del {
	min-width: 40px;
	min-height: 40px;
}
.jv-surface-phone .jv-grid-num {
	font-size: 14px;
	font-weight: 650;
}
.jv-surface-phone .jv-rows-changed-open,
.jv-surface-phone .jv-draft-addrow {
	min-height: 40px;
}
.jv-surface-phone .jv-draft-addrow {
	padding: 8px 4px;
	font-size: 14px;
}
.jv-surface-phone :deep(.jv-rowform-btn),
.jv-surface-phone :deep(.jv-rowform-close) {
	min-height: 40px;
	font-size: 14px;
}
.jv-surface-phone :deep(.jv-rowform-cols) {
	grid-template-columns: 1fr;
}
.jv-surface-phone :deep(.jv-rowform-label) {
	font-size: 11.5px;
}
/* the read-only totals keep room for their lock glyph (the rule above gives every input 10px) */
.jv-surface-phone .jv-draft-total-input {
	padding-right: 28px;
}
/* A row the server's message names ("Row 3: ..."): its open form carries a red edge instead of the
   usual one, and the sheet scrolls to it. */
.jv-grid-flagged > .jv-grid-opencell > :deep(.jv-rowform-block) {
	box-shadow: inset 3px 0 0 var(--red);
}
/* the row's remove glyph is drawn larger on the phone (the hit area above is 40px already) */
.jv-surface-phone .jv-grid-del {
	font-size: 20px;
	line-height: 1;
}
</style>
