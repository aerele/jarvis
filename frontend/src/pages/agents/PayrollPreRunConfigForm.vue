<template>
	<fieldset :disabled="saving" class="min-w-0">
		<p class="text-sm text-ink-gray-6">
			Choose one Payroll Entry and a named reviewer. Jarvis reads the company, period, and
			report cutoff from that entry. You cannot change those values here.
		</p>

		<div
			v-if="Object.keys(shownErrors).length"
			class="mt-3 rounded-lg border border-outline-gray-2 bg-surface-gray-1 p-3 text-sm text-ink-gray-7"
			role="status"
		>
			<p class="font-medium">Complete these settings before running</p>
			<ul class="mt-2 list-disc space-y-1 pl-5">
				<li v-for="(message, key) in shownErrors" :key="key">{{ message }}</li>
			</ul>
		</div>

		<div class="mt-4 space-y-5">
			<div>
				<label class="mb-1.5 block text-xs text-ink-gray-5">Payroll Entry</label>
				<div @focusin="entrySearch.prime()" @click="entrySearch.prime()">
					<Autocomplete
						:options="entrySearch.options.value"
						:modelValue="entryValue"
						placeholder="Search Payroll Entry…"
						@update:query="entrySearch.onQuery"
						@update:modelValue="onEntryPick"
					/>
				</div>
				<p class="mt-1.5 text-p-xs text-ink-gray-5">
					Select one readable entry. Jarvis verifies that it is not cancelled or fully
					submitted before it runs.
				</p>
			</div>

			<FormControl
				type="text"
				label="Payroll reviewer"
				description="The evaluator records this name on the worklist. It does not submit or edit payroll."
				:modelValue="form.reviewer_of_record"
				@update:modelValue="(value) => (form.reviewer_of_record = value || '')"
			/>
		</div>

		<div class="mt-4">
			<Button label="Save configuration" :loading="saving" @click="save" />
			<p class="mt-2 text-p-xs text-ink-gray-5">
				You can save a draft. Run Now stays unavailable until Jarvis validates the selected
				Payroll Entry as the run-as user.
			</p>
		</div>
	</fieldset>
</template>

<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { Autocomplete, Button, FormControl } from "frappe-ui";
import { searchLink } from "@/api";
import { useLinkSearch } from "@/composables/useLinkSearch";

const props = defineProps({
	config: { type: Object, default: () => ({}) },
	validationErrors: { type: Object, default: () => ({}) },
	saving: { type: Boolean, default: false },
});
const emit = defineEmits(["save", "dirty"]);

const form = reactive({ payroll_entry: "", reviewer_of_record: "" });
const initialState = ref("");

const entrySearch = useLinkSearch(
	(query) =>
		searchLink("Payroll Entry", query, 20, undefined, undefined, [
			["Payroll Entry", "docstatus", "!=", 2],
		]),
	{ mapper: (rows) => rows.map((row) => ({ label: row.value, value: row.value })) }
);

const entryValue = computed(() =>
	form.payroll_entry ? { label: form.payroll_entry, value: form.payroll_entry } : null
);
const shownErrors = computed(() => props.validationErrors || {});

function state() {
	return JSON.stringify(form);
}

function seed(config) {
	const source = config || {};
	form.payroll_entry = typeof source.payroll_entry === "string" ? source.payroll_entry : "";
	form.reviewer_of_record =
		typeof source.reviewer_of_record === "string" ? source.reviewer_of_record : "";
	initialState.value = state();
}

watch(
	() => props.config,
	(value) => seed(value),
	{ immediate: true, deep: true }
);
watch(form, () => emit("dirty", state() !== initialState.value), { deep: true, immediate: true });

function onEntryPick(option) {
	form.payroll_entry = option?.value ? String(option.value) : "";
}

function save() {
	const config = {};
	if (form.payroll_entry.trim()) config.payroll_entry = form.payroll_entry.trim();
	if (form.reviewer_of_record.trim()) config.reviewer_of_record = form.reviewer_of_record.trim();
	emit("save", config);
}

onBeforeUnmount(() => {
	entrySearch.cleanup();
});
</script>
