<template>
	<div ref="rootEl" class="min-w-0 max-w-full space-y-4" data-testid="intake-fields">
		<!-- Source -->
		<div class="flex min-w-0 flex-col gap-1.5">
			<label class="text-xs text-ink-gray-7">Source</label>
			<div class="flex flex-wrap gap-2">
				<Button
					v-for="s in SOURCES"
					:key="s.value"
					:label="s.label"
					:variant="form.intake_source === s.value ? 'solid' : 'subtle'"
					:disabled="disabled"
					@click="setSource(s.value)"
				/>
			</div>
		</div>

		<div
			v-if="form.intake_source === 'Folder'"
			data-pick="folder"
			class="flex min-w-0 flex-col gap-1.5"
		>
			<label class="text-xs text-ink-gray-7">Folder</label>
			<Autocomplete
				v-if="!disabled"
				:options="folderOptions"
				:modelValue="pickValue(form.intake_folder)"
				placeholder="Pick a folder…"
				@update:modelValue="
					(o) => {
						emit('update', { intake_folder: optValue(o) });
						refocus('folder');
					}
				"
			/>
			<FormControl v-else type="text" :modelValue="form.intake_folder" :disabled="true" />
		</div>
		<div v-else data-pick="record" class="flex min-w-0 flex-col gap-1.5">
			<label class="text-xs text-ink-gray-7">Record DocType</label>
			<Autocomplete
				v-if="!disabled"
				:options="doctypeOptions('record')"
				:loading="doctypeLoading('record')"
				:modelValue="pickValue(form.intake_record_doctype)"
				placeholder="Files attached to records of…"
				@update:query="(q) => doctypeSearch.record.onQuery(q)"
				@update:modelValue="
					(o) => {
						setRecordDoctype(o);
						refocus('record');
					}
				"
			/>
			<FormControl
				v-else
				type="text"
				:modelValue="form.intake_record_doctype"
				:disabled="true"
			/>
		</div>

		<div data-pick="target" class="flex min-w-0 flex-col gap-1.5">
			<label class="text-xs text-ink-gray-7">Create draft of</label>
			<Autocomplete
				v-if="!disabled"
				:options="doctypeOptions('target')"
				:loading="doctypeLoading('target')"
				:modelValue="pickValue(form.intake_target_doctype)"
				placeholder="Pick a DocType…"
				@update:query="(q) => doctypeSearch.target.onQuery(q)"
				@update:modelValue="
					(o) => {
						onTargetPick(o);
						refocus('target');
					}
				"
			/>
			<FormControl
				v-else
				type="text"
				:modelValue="form.intake_target_doctype"
				:disabled="true"
			/>
			<ErrorMessage :message="loadError" />
			<p v-if="loadingFields" class="text-p-sm text-ink-gray-7" role="status">
				Loading the fields of {{ form.intake_target_doctype }}…
			</p>
		</div>

		<template v-if="options">
			<!-- Fields -->
			<div class="flex min-w-0 flex-col gap-1.5" data-testid="intake-field-list">
				<label class="text-xs text-ink-gray-7">Fields to read</label>
				<div class="max-h-64 space-y-1.5 overflow-auto rounded-md border p-2">
					<FormControl
						v-for="f in options.fields"
						:key="f.fieldname"
						type="checkbox"
						:label="fieldLabel(f)"
						:modelValue="spec.fields.includes(f.fieldname)"
						:disabled="disabled"
						@update:modelValue="(v) => toggleField(f.fieldname, v)"
					/>
				</div>
			</div>

			<!-- Child table -->
			<div v-if="options.tables.length" class="flex min-w-0 flex-col gap-1.5">
				<FormControl
					type="select"
					label="Table rows"
					:options="tableOptions"
					:modelValue="spec.table ? spec.table.fieldname : ''"
					:disabled="disabled"
					@update:modelValue="onTablePick"
				/>
				<div
					v-if="pickedTable"
					class="max-h-48 space-y-1.5 overflow-auto rounded-md border p-2"
				>
					<FormControl
						v-for="c in pickedTable.columns"
						:key="c.fieldname"
						type="checkbox"
						:label="fieldLabel(c)"
						:modelValue="spec.table.columns.includes(c.fieldname)"
						:disabled="disabled"
						@update:modelValue="(v) => toggleColumn(c.fieldname, v)"
					/>
				</div>
			</div>

			<!-- Match links -->
			<div v-if="linkFields.length" class="flex min-w-0 flex-col gap-1.5">
				<label class="text-xs text-ink-gray-7">Match to existing records</label>
				<div class="space-y-1.5 rounded-md border p-2">
					<FormControl
						v-for="f in linkFields"
						:key="f.fieldname"
						type="checkbox"
						:label="'Match: ' + f.label"
						:modelValue="spec.match.includes(f.fieldname)"
						:disabled="disabled"
						@update:modelValue="(v) => toggleMatch(f.fieldname, v)"
					/>
				</div>
			</div>

			<!-- Defaults -->
			<div v-if="defaultFields.length" class="flex flex-col gap-3">
				<label class="text-xs text-ink-gray-7">
					Fixed values for mandatory fields that are not read from the file
				</label>
				<div
					v-for="f in defaultFields"
					:key="f.fieldname"
					:data-testid="'default-' + f.fieldname"
				>
					<template v-if="f.fieldtype === 'Link'">
						<label class="mb-1.5 block text-xs text-ink-gray-7">{{ f.label }}</label>
						<Autocomplete
							v-if="!disabled"
							:options="linkOptions(f)"
							:loading="linkSearch(f).loading.value"
							:modelValue="pickValue(spec.defaults[f.fieldname])"
							:placeholder="'Pick a ' + f.options + '…'"
							@update:query="(q) => linkSearch(f).onQuery(q)"
							@update:modelValue="(o) => setDefault(f.fieldname, optValue(o))"
						/>
						<FormControl
							v-else
							type="text"
							:modelValue="spec.defaults[f.fieldname]"
							:disabled="true"
						/>
					</template>
					<FormControl
						v-else-if="f.fieldtype === 'Select'"
						type="select"
						:label="f.label"
						:options="selectOptions(f)"
						:modelValue="spec.defaults[f.fieldname] || ''"
						:disabled="disabled"
						@update:modelValue="(v) => setDefault(f.fieldname, v)"
					/>
					<FormControl
						v-else
						type="text"
						:label="f.label"
						:modelValue="spec.defaults[f.fieldname] || ''"
						:disabled="disabled"
						@update:modelValue="(v) => setDefault(f.fieldname, v)"
					/>
				</div>
			</div>

			<p
				v-if="uncovered.length"
				class="text-p-sm text-ink-amber-3"
				role="alert"
				data-testid="intake-uncovered"
			>
				Needs a value or a default: {{ uncovered.join(", ") }}
			</p>
		</template>

		<!-- Instructions -->
		<div class="flex min-w-0 flex-col gap-1.5">
			<FormControl
				type="textarea"
				label="Instructions"
				:rows="4"
				placeholder="e.g. One table row is one item line. Machine codes look like M-07."
				:maxlength="MAX_INSTRUCTIONS"
				:modelValue="form.intake_instructions"
				:disabled="disabled"
				@update:modelValue="
					(v) => emit('update', { intake_instructions: clampInstructions(v) })
				"
			/>
			<p
				class="text-p-xs"
				:class="
					instructionsLength >= MAX_INSTRUCTIONS ? 'text-ink-red-4' : 'text-ink-gray-7'
				"
				aria-live="polite"
				data-testid="instructions-count"
			>
				{{ instructionsLength }} / {{ MAX_INSTRUCTIONS }}
			</p>
		</div>

		<FormControl
			type="number"
			label="Daily limit"
			description="Max files read per day for this rule"
			:modelValue="form.llm_daily_cap"
			:disabled="disabled"
			@update:modelValue="(v) => emit('update', { llm_daily_cap: v })"
		/>

		<div class="flex min-w-0 flex-col gap-1.5">
			<FormControl
				type="checkbox"
				label="Allow incomplete drafts"
				:modelValue="!!form.allow_incomplete_drafts"
				:disabled="disabled"
				@update:modelValue="(v) => emit('update', { allow_incomplete_drafts: v ? 1 : 0 })"
			/>
			<!-- frappe-ui's checkbox ignores the description prop; same classes as the LLM kind's help line -->
			<p class="text-p-xs text-ink-gray-7">
				Create the draft even when a mandatory field could not be read. Missing fields are
				listed on the draft. Submit still needs every mandatory field.
			</p>
		</div>

		<!-- Write-back (record source only) -->
		<div
			v-if="form.intake_source === 'Record'"
			class="flex flex-col gap-3"
			data-testid="writeback"
		>
			<FormControl
				type="select"
				label="Status field on the record"
				:options="recordFieldOptions(STATUS_TYPES)"
				:modelValue="form.intake_status_field || ''"
				:disabled="disabled"
				@update:modelValue="(v) => emit('update', { intake_status_field: v })"
			/>
			<FormControl
				type="select"
				label="Draft link field on the record"
				:options="recordFieldOptions(LINK_TYPES)"
				:modelValue="form.intake_link_field || ''"
				:disabled="disabled"
				@update:modelValue="(v) => emit('update', { intake_link_field: v })"
			/>
		</div>
	</div>
</template>

<script setup>
// The Intake kind's form body, rendered by TriggerDetail when action_type is
// "Intake". It owns no state of the trigger: it reads `form` and emits
// `update` with a patch ({key: value}) that TriggerDetail applies to its form.
import { ref, computed, nextTick, watch, onBeforeUnmount } from "vue";
import { Autocomplete, Button, ErrorMessage, FormControl } from "frappe-ui";
import { searchLink } from "@/api";
import { useLinkSearch } from "@/composables/useLinkSearch";
import * as apiTriggers from "@/api/triggers";
import { errMessage } from "@/lib/errors";
import { emptySpec, parseSpec, uncoveredLabels } from "./intakeSpec";

const props = defineProps({
	form: { type: Object, required: true },
	disabled: { type: Boolean, default: false },
	// A non-manager: the field lists are manager-only endpoints, so nothing is loaded and
	// the redacted spec shows empty.
	readOnly: { type: Boolean, default: false },
});
const emit = defineEmits(["update"]);

const SOURCES = [
	{ value: "Folder", label: "Folder" },
	{ value: "Record", label: "Record" },
];
const MAX_INSTRUCTIONS = 4000;
// What the server accepts for the write-back (jarvis_trigger._validate_intake_record): a
// Select would refuse "Needs Review", a Link to another doctype would refuse the draft name.
const STATUS_TYPES = ["Data", "Small Text"];
const LINK_TYPES = ["Link", "Data", "Dynamic Link"];

// The server refuses more than this; stop at the limit here instead of failing on save
// (a paste past the limit is cut, not rejected later).
const clampInstructions = (v) => String(v ?? "").slice(0, MAX_INSTRUCTIONS);
const instructionsLength = computed(() => (props.form.intake_instructions || "").length);

const spec = computed(() => parseSpec(props.form.intake_spec));
const patchSpec = (next) => emit("update", { intake_spec: next });

// A picker closes on pick and the page re-renders around it: put focus back on its button.
const rootEl = ref(null);
function refocus(which) {
	nextTick(() => {
		const btn = rootEl.value && rootEl.value.querySelector(`[data-pick="${which}"] button`);
		if (btn) btn.focus();
	});
}

const pickValue = (v) => (v ? { label: v, value: v } : null);
const optValue = (o) => (o && o.value ? String(o.value) : "");
const fieldLabel = (f) => (f.mandatory ? `${f.label} (required)` : f.label);

// The other source's picks mean nothing under this one, so they are cleared.
function setSource(value) {
	if (props.form.intake_source === value) return;
	emit(
		"update",
		value === "Folder"
			? {
					intake_source: value,
					intake_record_doctype: "",
					intake_status_field: "",
					intake_link_field: "",
			  }
			: { intake_source: value, intake_folder: "" }
	);
}

// The status and link picks name fields of the old doctype, so a new doctype drops them.
function setRecordDoctype(o) {
	const value = optValue(o);
	if (value === (props.form.intake_record_doctype || ""))
		return emit("update", { intake_record_doctype: value });
	emit("update", {
		intake_record_doctype: value,
		intake_status_field: "",
		intake_link_field: "",
	});
}

// ── folders ──────────────────────────────────────────────────────────────────
const folderOptions = ref([]);
function loadFolders() {
	if (props.readOnly) return;
	apiTriggers
		.listIntakeFolders()
		.then((rows) => (folderOptions.value = (rows || []).map((n) => ({ label: n, value: n }))))
		.catch(() => (folderOptions.value = []));
}
// `readOnly` is true until the page knows the admin's rights, so the loads wait for it.
watch(() => props.readOnly, loadFolders, { immediate: true });

// ── DocType pickers (search_link, like the page's own DocType picker) ────────
// useLinkSearch debounces and drops a slow earlier answer, so the list always matches
// what is typed (latest wins), and it says when a search is still running.
const docMap = (rows) => (rows || []).map((x) => ({ label: x.value, value: x.value }));
const doctypeSearch = {
	record: useLinkSearch((q) => searchLink("DocType", q), { mapper: docMap }),
	target: useLinkSearch((q) => searchLink("DocType", q), { mapper: docMap }),
};
const doctypeOptions = (which) => doctypeSearch[which].options.value;
const doctypeLoading = (which) => doctypeSearch[which].loading.value;
doctypeSearch.record.prime();
doctypeSearch.target.prime();
onBeforeUnmount(() => Object.values(doctypeSearch).forEach((x) => x.cleanup()));

// ── field options of the target and of the record DocType ────────────────────
const options = ref(null);
const loadError = ref("");
const loadingFields = ref(false);
const recordOptions = ref(null);
let seq = 0;

async function loadTarget(doctype) {
	const mine = ++seq;
	options.value = null;
	loadError.value = "";
	loadingFields.value = false;
	if (!doctype || props.readOnly) return;
	loadingFields.value = true;
	try {
		const res = await apiTriggers.getIntakeFieldOptions(doctype);
		if (mine !== seq) return;
		options.value = { fields: res?.fields || [], tables: res?.tables || [] };
	} catch (e) {
		if (mine === seq) loadError.value = errMessage(e);
	} finally {
		if (mine === seq) loadingFields.value = false;
	}
}

let recordSeq = 0;
async function loadRecord(doctype) {
	const mine = ++recordSeq;
	recordOptions.value = null;
	if (!doctype || props.readOnly) return;
	try {
		const res = await apiTriggers.getIntakeFieldOptions(doctype);
		if (mine === recordSeq) recordOptions.value = res;
	} catch (e) {
		// the record DocType's own error shows on save; the write-back lists stay empty
	}
}

watch(
	() => [props.form.intake_target_doctype, props.readOnly],
	([d]) => loadTarget(d),
	{
		immediate: true,
	}
);
watch(
	() => [
		props.form.intake_source === "Record" ? props.form.intake_record_doctype : "",
		props.readOnly,
	],
	([d]) => loadRecord(d),
	{ immediate: true }
);

// A fresh spec starts with every mandatory field ticked, so the admin only adds.
// Also re-run when `disabled` clears: the page may still be reading the admin's rights.
function precheckMandatory() {
	const s = spec.value;
	if (!options.value || props.disabled || s.fields.length || s.table) return;
	const mandatory = options.value.fields.filter((f) => f.mandatory).map((f) => f.fieldname);
	if (mandatory.length) patchSpec({ ...s, fields: mandatory });
}

watch(() => [options.value, props.disabled], precheckMandatory);

function onTargetPick(opt) {
	const value = optValue(opt);
	if (value === props.form.intake_target_doctype) return;
	// another DocType: the old picks mean nothing there
	emit("update", { intake_target_doctype: value, intake_spec: emptySpec() });
}

// ── fields, table, match ─────────────────────────────────────────────────────
const toggled = (list, name, on) =>
	on ? (list.includes(name) ? list : [...list, name]) : list.filter((x) => x !== name);

function toggleField(name, on) {
	const s = spec.value;
	patchSpec({ ...s, fields: toggled(s.fields, name, on) });
}

const tableOptions = computed(() => [
	{ label: "No table", value: "" },
	...(options.value?.tables || []).map((t) => ({ label: t.label, value: t.fieldname })),
]);
const pickedTable = computed(() =>
	spec.value.table
		? (options.value?.tables || []).find((t) => t.fieldname === spec.value.table.fieldname)
		: null
);

function onTablePick(fieldname) {
	const s = spec.value;
	const table = (options.value?.tables || []).find((t) => t.fieldname === fieldname);
	patchSpec({
		...s,
		table: table
			? {
					fieldname,
					columns: table.columns.filter((c) => c.mandatory).map((c) => c.fieldname),
			  }
			: null,
	});
}

function toggleColumn(name, on) {
	const s = spec.value;
	patchSpec({ ...s, table: { ...s.table, columns: toggled(s.table.columns, name, on) } });
}

const linkFields = computed(() =>
	(options.value?.fields || []).filter(
		(f) => f.fieldtype === "Link" && spec.value.fields.includes(f.fieldname)
	)
);

function toggleMatch(name, on) {
	const s = spec.value;
	patchSpec({ ...s, match: toggled(s.match, name, on) });
}

// ── defaults ─────────────────────────────────────────────────────────────────
const defaultFields = computed(() =>
	(options.value?.fields || []).filter(
		(f) => f.mandatory && !spec.value.fields.includes(f.fieldname)
	)
);

function setDefault(name, value) {
	const s = spec.value;
	patchSpec({ ...s, defaults: { ...s.defaults, [name]: value } });
}

const selectOptions = (f) => [
	{ label: "", value: "" },
	...String(f.options || "")
		.split("\n")
		.map((o) => o.trim())
		.filter(Boolean)
		.map((o) => ({ label: o, value: o })),
];

const linkSearches = {};
function linkSearch(f) {
	if (!linkSearches[f.fieldname])
		linkSearches[f.fieldname] = useLinkSearch((q) => searchLink(f.options, q), {
			mapper: docMap,
		});
	return linkSearches[f.fieldname];
}
const linkOptions = (f) => linkSearch(f).options.value;
onBeforeUnmount(() => Object.values(linkSearches).forEach((x) => x.cleanup()));

// ── strict-mode coverage ─────────────────────────────────────────────────────
const uncovered = computed(() =>
	props.form.allow_incomplete_drafts ? [] : uncoveredLabels(spec.value, options.value)
);

// ── write-back lists ─────────────────────────────────────────────────────────
function recordFieldOptions(types) {
	const rows = (recordOptions.value?.fields || []).filter(
		(f) =>
			types.includes(f.fieldtype) &&
			(f.fieldtype !== "Link" || f.options === props.form.intake_target_doctype)
	);
	return [
		{ label: "None", value: "" },
		...rows.map((f) => ({ label: f.label, value: f.fieldname })),
	];
}
</script>
