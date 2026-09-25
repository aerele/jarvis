<template>
	<!-- One record's fields as a small form (Edit & create on a held File Box
	     create). Controlled: every edit emits update:values with a new object.
	     Values and labels are text-interpolated only (model-derived). -->
	<div class="flex flex-col gap-3">
		<div v-for="f in mainFields" :key="f.key" class="flex flex-col gap-1">
			<label :for="inputId(f.key)" class="text-sm text-ink-gray-7">
				{{ f.label
				}}<span v-if="f.reqd" class="text-ink-red-4" aria-hidden="true"> *</span>
			</label>
			<div
				v-if="locked[f.key]"
				:id="inputId(f.key)"
				class="rounded bg-surface-gray-2 px-2 py-1.5 text-base text-ink-gray-7"
				:aria-describedby="noteId(f.key)"
			>
				{{ f.value || "—" }}
			</div>
			<FieldControl
				v-else
				:id="inputId(f.key)"
				:field="f"
				:invalid="flagged(f.key)"
				:error="!!errors[f.key]"
				:describedby="note(f.key) ? noteId(f.key) : undefined"
				:suggestions="linkOptions[f.key] || []"
				@update="(v) => setMain(f, v)"
			/>
			<p v-if="note(f.key)" :id="noteId(f.key)" class="text-xs" :class="noteClass(f.key)">
				{{ note(f.key) }}
			</p>
		</div>

		<fieldset v-for="t in tableViews" :key="t.fieldname" class="rounded border p-3">
			<legend class="px-1 text-sm font-medium text-ink-gray-8">{{ t.label }}</legend>
			<div
				v-for="(row, ri) in t.rows"
				:key="ri"
				role="group"
				:aria-label="__('{0} row {1}', [t.label, ri + 1])"
				class="flex flex-col gap-2 border-t py-2 first:border-t-0"
			>
				<div class="text-xs text-ink-gray-5">{{ __("Row {0}", [ri + 1]) }}</div>
				<div v-for="c in row" :key="c.key" class="flex flex-col gap-1">
					<label :for="inputId(c.key)" class="text-sm text-ink-gray-7">{{
						c.label
					}}</label>
					<div
						v-if="locked[c.key]"
						:id="inputId(c.key)"
						class="rounded bg-surface-gray-2 px-2 py-1.5 text-base text-ink-gray-7"
						:aria-describedby="noteId(c.key)"
					>
						{{ c.value || "—" }}
					</div>
					<FieldControl
						v-else
						:id="inputId(c.key)"
						:field="c"
						:invalid="flagged(c.key)"
						:error="!!errors[c.key]"
						:describedby="note(c.key) ? noteId(c.key) : undefined"
						@update="(v) => setCell(t.fieldname, ri, c, v)"
					/>
					<p
						v-if="note(c.key)"
						:id="noteId(c.key)"
						class="text-xs"
						:class="noteClass(c.key)"
					>
						{{ note(c.key) }}
					</p>
				</div>
			</div>
		</fieldset>
	</div>
</template>

<script setup>
// Standalone record form for the Approval Board's Edit & create (PR-2d). Props:
// meta = actions_api.get_doctype_form_meta; values = the proposed values; needsInput
// = this record's missing fields ({fieldname, parentfield?, idx?, field?});
// locked = {key: reason} (read-only, reason shown); errors = {key: message} from
// the server. A child cell's key is "table.idx.field". Shows the proposed fields
// plus the missing ones; a table adds any column a missing field needs.
import { computed, reactive, getCurrentInstance, onBeforeUnmount } from "vue";
import FieldControl from "@/components/forms/FieldControl.vue";
import { searchLink } from "@/api";
import { coerceOut } from "@/lib/draftApply";
import { panelField } from "@/lib/docFields";
import { fieldKey } from "@/lib/heldEdit";
import { __ } from "@/lib/i18n";

const props = defineProps({
	meta: { type: Object, required: true },
	values: { type: Object, required: true },
	needsInput: { type: Array, default: () => [] },
	locked: { type: Object, default: () => ({}) },
	errors: { type: Object, default: () => ({}) },
});
const emit = defineEmits(["update:values"]);

const uid = `dff-${getCurrentInstance().uid}`;
const inputId = (key) => `${uid}-${key.replace(/\./g, "-")}`;
const noteId = (key) => `${inputId(key)}-note`;

const needKeys = computed(() => new Set(props.needsInput.map(fieldKey)));
const empty = (v) => v == null || String(v).trim() === "";

const mainFields = computed(() => {
	const wanted = new Set([
		...Object.keys(props.values),
		...props.needsInput.filter((e) => !e.parentfield).map((e) => e.fieldname),
	]);
	return (props.meta.fields || [])
		.filter((f) => f.fieldtype !== "Table" && wanted.has(f.fieldname))
		.map((f) => ({ ...panelField(f, props.values[f.fieldname]), key: f.fieldname }));
});

const tableViews = computed(() => {
	const out = [];
	for (const [fieldname, spec] of Object.entries(props.meta.tables || {})) {
		const rows = props.values[fieldname];
		if (!Array.isArray(rows) || !rows.length) continue;
		const columns = [...(spec.columns || [])];
		for (const e of props.needsInput) {
			const known = columns.some((c) => c.fieldname === e.fieldname);
			if (e.parentfield === fieldname && e.field && !known) columns.push(e.field);
		}
		out.push({
			fieldname,
			label: spec.label || fieldname,
			rows: rows.map((row, ri) =>
				columns.map((c) => ({
					...panelField(c, (row || {})[c.fieldname]),
					key: `${fieldname}.${ri + 1}.${c.fieldname}`,
				}))
			),
		});
	}
	return out;
});

function currentValue(key) {
	const [field, idx, cell] = key.split(".");
	if (!cell) return props.values[field];
	const row = (props.values[field] || [])[Number(idx) - 1];
	return row ? row[cell] : undefined;
}
function flagged(key) {
	return !!props.errors[key] || (needKeys.value.has(key) && empty(currentValue(key)));
}
function note(key) {
	if (props.locked[key]) return props.locked[key];
	if (props.errors[key]) return props.errors[key];
	return flagged(key) ? __("Required: fill this in.") : "";
}
function noteClass(key) {
	if (props.locked[key]) return "text-ink-gray-5";
	return props.errors[key] ? "text-ink-red-4" : "text-ink-amber-3";
}

function setMain(f, value) {
	emit("update:values", { ...props.values, [f.fieldname]: coerceOut({ ...f, value }) });
	if (f.control === "link" && f.options) suggest(f.key, f.options, value);
}
function setCell(table, index, cell, value) {
	const rows = (props.values[table] || []).map((r, i) =>
		i === index ? { ...r, [cell.fieldname]: coerceOut({ ...cell, value }) } : r
	);
	emit("update:values", { ...props.values, [table]: rows });
}

// Link fields: search_link suggestions into the control's <datalist>, debounced.
const linkOptions = reactive({});
const timers = {};
function suggest(key, doctype, value) {
	clearTimeout(timers[key]);
	timers[key] = setTimeout(async () => {
		try {
			const rows = (await searchLink(doctype, value, 10)) || [];
			linkOptions[key] = rows.map((r) => String(r.value));
		} catch {
			linkOptions[key] = [];
		}
	}, 250);
}
onBeforeUnmount(() => Object.values(timers).forEach(clearTimeout));
</script>
