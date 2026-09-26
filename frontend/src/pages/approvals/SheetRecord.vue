<template>
	<!-- One record of an approval sheet: its key values, what it still needs, and a
	     Create · Edit · Use existing · Skip choice (an update: Apply · Edit · Skip).
	     Values are model-derived from a dropped file: text interpolation only. -->
	<li
		:id="rowId"
		tabindex="-1"
		class="py-3 outline-none focus-visible:ring-2 focus-visible:ring-outline-gray-3"
		:aria-labelledby="rowId + '-title'"
		:aria-describedby="describedBy || undefined"
	>
		<div class="flex flex-wrap items-start gap-x-3 gap-y-2">
			<div class="min-w-0 flex-1">
				<div class="flex flex-wrap items-center gap-2">
					<span class="text-sm tabular-nums text-ink-gray-5">#{{ n }}</span>
					<span
						:id="rowId + '-title'"
						class="min-w-0 truncate text-base font-medium text-ink-gray-9"
						>{{ record.title || record.name || record.doctype }}</span
					>
					<Badge variant="subtle" theme="gray" :label="record.doctype" />
					<Badge
						v-if="record.op === 'update'"
						variant="subtle"
						theme="blue"
						:label="__('Update')"
					/>
					<Badge
						v-if="cascadedBy >= 0"
						variant="subtle"
						theme="gray"
						:label="__('Skipped with #{0}', [cascadedBy + 1])"
					/>
					<Badge
						v-else-if="fix && active"
						variant="subtle"
						theme="red"
						:label="__('Needs a fix')"
					/>
					<Badge
						v-else-if="missing.length && active"
						variant="subtle"
						theme="orange"
						:label="__('Needs input')"
					/>
				</div>
				<dl v-if="fields.length" class="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-sm">
					<div v-for="f in fields" :key="f.key" class="flex min-w-0 gap-1">
						<dt class="shrink-0 text-ink-gray-5">{{ f.label }}</dt>
						<dd class="truncate text-ink-gray-7">{{ f.value }}</dd>
					</div>
				</dl>
				<p v-if="record.depends_on.length" class="mt-1 text-sm text-ink-gray-5">
					{{ __("Needs {0}", [record.depends_on.map((d) => "#" + (d + 1)).join(", ")]) }}
				</p>
				<p v-if="cascadedBy >= 0" class="mt-1 text-sm text-ink-amber-3">
					{{ cascadeReason }}
				</p>
				<p
					v-else-if="action === 'skip' && cascadeCount"
					class="mt-1 text-sm text-ink-amber-3"
				>
					{{
						__("Skipping this also skips {0} {1} on it.", [
							cascadeCount,
							cascadeCount === 1 ? "record that depends" : "records that depend",
						])
					}}
				</p>
				<p v-if="fix && active" :id="rowId + '-fix'" class="mt-1 text-sm text-ink-red-4">
					{{ __("Needs a fix: {0}", [fix.message]) }}
				</p>
				<p
					v-if="missing.length && active"
					:id="rowId + '-missing'"
					class="mt-1 text-sm text-ink-amber-3"
				>
					{{ __("Missing: {0}", [missingText]) }}
				</p>
				<p
					v-if="action === 'use_existing' && choice.existing"
					class="mt-1 text-sm text-ink-gray-7"
				>
					{{
						__("Uses the existing {0} {1}", [
							record.doctype,
							choice.existingLabel || choice.existing,
						])
					}}
				</p>
				<p
					v-if="issueHint && cascadedBy < 0"
					:id="rowId + '-issue'"
					class="mt-1 text-sm text-ink-gray-6"
				>
					{{ issueHint }}
				</p>
				<p v-if="error" :id="rowId + '-error'" class="mt-1 text-sm text-ink-red-4">
					{{ error }}
				</p>
			</div>
			<div
				role="group"
				:aria-label="'Decision for #' + n"
				class="inline-flex shrink-0 rounded bg-surface-gray-2 p-0.5"
			>
				<button
					v-for="a in actions"
					:key="a"
					:ref="(el) => (buttons[a] = el)"
					type="button"
					class="rounded px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-outline-gray-3"
					:class="
						action === a
							? 'bg-surface-white text-ink-gray-9 shadow-sm'
							: 'text-ink-gray-6 hover:text-ink-gray-8'
					"
					:aria-pressed="action === a ? 'true' : 'false'"
					:aria-expanded="panelOf(a) ? String(panel === panelOf(a)) : undefined"
					:aria-controls="panelOf(a) ? rowId + '-' + panelOf(a) : undefined"
					@click="pick(a)"
				>
					{{ ACTION_LABEL[a] }}
				</button>
			</div>
		</div>

		<div
			v-if="panel === 'edit'"
			:id="rowId + '-edit'"
			ref="panelEl"
			tabindex="-1"
			role="region"
			:aria-label="'Edit #' + n"
			class="mt-3 rounded-lg border p-3 outline-none"
			@keydown.esc.stop.prevent="close('edit')"
		>
			<JvSpinner v-if="form.loading" :label="__('Loading the form…')" />
			<template v-else>
				<div
					v-if="record.op === 'create'"
					class="mb-3 flex flex-wrap items-center justify-end gap-2"
				>
					<a
						:href="
							deskNewUrl(
								record.doctype,
								choice.values || record.values,
								record.secret
							)
						"
						target="_blank"
						rel="noopener"
						class="text-sm text-ink-gray-6 underline"
						>{{ __("Open full form in Desk") }}</a
					>
				</div>
				<DocFieldsForm
					v-if="form.meta"
					:meta="form.meta"
					:values="choice.values || record.values"
					:needs-input="record.needs_input"
					:locked="locked"
					@update:values="(v) => emit('choose', record.index, { values: v })"
				/>
				<p v-else role="alert" class="text-sm text-ink-red-4">
					{{ __("This form couldn't be loaded.") }}
					<template v-if="record.op === 'create'">
						{{ __("Create it in the full form in Desk, then choose Use existing.") }}
					</template>
				</p>
			</template>
			<div class="mt-3 flex flex-wrap items-center gap-2">
				<Button variant="subtle" size="sm" :label="__('Done')" @click="close('edit')" />
				<Button variant="ghost" size="sm" :label="__('Undo changes')" @click="undo" />
			</div>
		</div>

		<div
			v-if="panel === 'existing'"
			:id="rowId + '-existing'"
			ref="panelEl"
			tabindex="-1"
			role="region"
			:aria-labelledby="rowId + '-existing-title'"
			class="mt-3 rounded-lg border p-3 outline-none"
			@keydown.esc.stop.prevent="close('existing')"
		>
			<div :id="rowId + '-existing-title'" class="text-sm font-medium text-ink-gray-8">
				{{ __("Use an existing {0} instead", [record.doctype]) }}
			</div>
			<JvSpinner v-if="found.loading" :label="__('Looking for matches…')" />
			<p v-else-if="found.error" role="alert" class="mt-2 text-sm text-ink-red-4">
				{{ found.error }}
			</p>
			<ul v-else-if="found.rows.length" class="mt-2 flex flex-col divide-y">
				<li v-for="c in found.rows" :key="c.name" class="flex items-center gap-3 py-2">
					<div class="min-w-0 flex-1">
						<div class="truncate text-base text-ink-gray-9">
							{{ c.title || c.name }}
						</div>
						<div class="truncate text-sm text-ink-gray-5">
							{{ c.name }}<template v-if="c.gstin"> · {{ c.gstin }}</template>
						</div>
					</div>
					<Button
						variant="subtle"
						size="sm"
						:label="choice.existing === c.name ? __('Chosen') : __('Use this')"
						:aria-label="'Use ' + (c.title || c.name)"
						:aria-pressed="choice.existing === c.name ? 'true' : 'false'"
						@click="useExisting(c.name, c.title)"
					/>
				</li>
			</ul>
			<p v-else class="mt-2 text-sm text-ink-gray-5">
				{{ __("No matching {0} found. Search for one below.", [record.doctype]) }}
			</p>
			<div class="mt-3" role="group" :aria-label="'Search ' + record.doctype">
				<Autocomplete
					:options="linkSearch.options.value"
					:loading="linkSearch.loading.value"
					:modelValue="null"
					:placeholder="'Search ' + record.doctype + '…'"
					@update:query="linkSearch.onQuery"
					@update:modelValue="(o) => o && useExisting(o.value, o.label)"
				/>
			</div>
		</div>
	</li>
</template>

<script setup>
// A row of SheetDetail. The choice lives in the sheet (the `choose` event patches
// it), so a collapsed or re-rendered row loses nothing; which panel is open is
// the row's own. Candidates and form meta load lazily through the sheet's caches.
import { ref, reactive, computed, watch, nextTick, onBeforeUnmount } from "vue";
import { Autocomplete, Badge, Button } from "frappe-ui";
import JvSpinner from "@/components/JvSpinner.vue";
import DocFieldsForm from "@/components/forms/DocFieldsForm.vue";
import { searchLink } from "@/api";
import { useLinkSearch } from "@/composables/useLinkSearch";
import { deskNewUrl, stillMissing } from "@/lib/heldEdit";
import { missingSummary } from "@/lib/heldActions";
import { errMessage } from "@/lib/errors";
import { __ } from "@/lib/i18n";
import { ACTION_LABEL, actionsFor, fixOf, keyFields } from "@/lib/sheet";

const props = defineProps({
	sheet: { type: String, required: true },
	record: { type: Object, required: true },
	choice: { type: Object, required: true },
	issue: { type: String, default: "" },
	error: { type: String, default: "" },
	cascadedBy: { type: Number, default: -1 },
	cascadeReason: { type: String, default: "" },
	cascadeCount: { type: Number, default: 0 },
	formMeta: { type: Function, required: true },
	loadCandidates: { type: Function, required: true },
});
const emit = defineEmits(["choose"]);

const HINTS = {
	fix: __("Use Edit to fix it, use an existing one, or skip it."),
	fix_unchanged: __("Change the value it needs, or skip it."),
	missing: __("Use Edit to fill it in, or skip it."),
	existing: __("Pick the existing record to use."),
};

const n = computed(() => props.record.index + 1);
const rowId = computed(() => `sheet-${props.sheet}-r${props.record.index}`);
const actions = computed(() => actionsFor(props.record));
const action = computed(() => props.choice.action);
const active = computed(
	() => props.cascadedBy < 0 && action.value !== "skip" && action.value !== "use_existing"
);
const fields = computed(() => keyFields(props.record));
const fix = computed(() => fixOf(props.record));
const missing = computed(() =>
	stillMissing(
		props.record.needs_input,
		props.record.index,
		action.value === "edit" && props.choice.values ? props.choice.values : props.record.values
	)
);
const missingText = computed(() => missingSummary(missing.value.map((e) => e.label)));
const issueHint = computed(() => HINTS[props.issue] || "");
const describedBy = computed(() =>
	[
		props.error && "-error",
		fix.value && active.value && "-fix",
		missing.value.length && active.value && "-missing",
		issueHint.value && props.cascadedBy < 0 && "-issue",
	]
		.filter(Boolean)
		.map((s) => rowId.value + s)
		.join(" ")
);
// Secrets can't be edited here: shown locked, like the held Edit & create.
const locked = computed(() => ({
	...(props.record.locked || {}),
	...Object.fromEntries(
		(props.record.secret || []).map((k) => [k, __("Hidden: a secret value.")])
	),
}));

const buttons = {};
const panelEl = ref(null);
const panel = ref(""); // "" | "edit" | "existing"
const panelOf = (a) => (a === "edit" ? "edit" : a === "use_existing" ? "existing" : "");
// a choice made elsewhere (bulk, set for all) closes a panel it no longer fits
watch(action, (a) => {
	if (panel.value && panel.value !== panelOf(a)) panel.value = "";
});

async function open(which) {
	panel.value = which;
	await nextTick();
	if (panelEl.value && panelEl.value.focus) panelEl.value.focus();
	if (which === "edit") loadForm();
	else loadFound();
}
function close(which) {
	if (panel.value !== which) return;
	panel.value = "";
	const btn = buttons[which === "edit" ? "edit" : "use_existing"];
	nextTick(() => btn && btn.focus && btn.focus());
}

function pick(a) {
	const which = panelOf(a);
	if (which && action.value === a) return panel.value === which ? close(which) : open(which);
	const patch = { action: a };
	if (a === "edit" && !props.choice.values)
		patch.values = JSON.parse(JSON.stringify(props.record.values || {}));
	emit("choose", props.record.index, patch);
	if (which) open(which);
	else panel.value = "";
}

function undo() {
	emit("choose", props.record.index, {
		values: JSON.parse(JSON.stringify(props.record.values || {})),
	});
}

const form = reactive({ loading: false, meta: null });
async function loadForm() {
	if (form.meta) return;
	form.loading = true;
	try {
		form.meta = await props.formMeta(props.record.doctype);
	} catch {
		form.meta = null;
	} finally {
		form.loading = false;
	}
}

const found = reactive({ loading: false, loaded: false, rows: [], error: "" });
async function loadFound() {
	if (found.loaded || found.loading) return;
	found.loading = true;
	found.error = "";
	try {
		found.rows = (await props.loadCandidates(props.record.index)) || [];
		found.loaded = true;
	} catch (e) {
		found.error = errMessage(e, __("Matches could not be loaded. Search below instead."));
	} finally {
		found.loading = false;
	}
}

function useExisting(name, label) {
	emit("choose", props.record.index, {
		action: "use_existing",
		existing: String(name),
		existingLabel: label && label !== name ? `${label} (${name})` : "",
	});
	close("existing");
}

const linkSearch = useLinkSearch((q) => searchLink(props.record.doctype, q, 10), {
	mapper: (rows) =>
		rows.map((r) => ({
			label: r.label || r.value,
			value: String(r.value),
			description: r.label && r.label !== r.value ? String(r.value) : r.description || "",
		})),
});
onBeforeUnmount(() => linkSearch.cleanup());
</script>
