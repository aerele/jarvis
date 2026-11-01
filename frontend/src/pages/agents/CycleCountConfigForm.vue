<template>
	<fieldset :disabled="saving" class="min-w-0">
		<p class="text-sm text-ink-gray-6">
			Set the scope for a human physical-count plan. A run proposes a plan only. It does not
			record a count, post stock, or change inventory.
		</p>
		<div class="mt-3 rounded-lg bg-surface-gray-1 p-3 text-sm text-ink-gray-6">
			<p>
				Use current records. Choose up to 10 enabled leaf warehouses. The named reviewer is
				set when the agent runs.
			</p>
			<Button
				class="mt-2"
				label="Fill empty settings"
				:loading="defaultsLoading"
				@click="fillEmptySettings"
			/>
			<p v-if="defaultsMessage" class="mt-2">{{ defaultsMessage }}</p>
		</div>

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
				<label class="mb-1.5 block text-xs text-ink-gray-5">Company</label>
				<div @focusin="companySearch.prime()" @click="companySearch.prime()">
					<Autocomplete
						:options="companySearch.options.value"
						:modelValue="companyValue"
						placeholder="Search Company…"
						@update:query="companySearch.onQuery"
						@update:modelValue="onCompanyPick"
					/>
				</div>
				<p class="mt-1.5 text-p-xs text-ink-gray-5">
					Select the company whose readable warehouses you will count.
				</p>
				<ErrorMessage v-if="shownErrors.company" :message="shownErrors.company" />
			</div>

			<WarehousePicker
				v-model="form.warehouses"
				:options="warehouseOptions"
				:loading="defaultsLoading"
				:disabled="!form.company"
			/>
			<ErrorMessage v-if="shownErrors.warehouses" :message="shownErrors.warehouses" />

			<div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
				<FormControl
					type="date"
					label="Movement evidence from"
					:modelValue="form.from_date"
					@update:modelValue="(v) => (form.from_date = v)"
				/>
				<FormControl
					type="date"
					label="Movement evidence to"
					:modelValue="form.to_date"
					@update:modelValue="(v) => (form.to_date = v)"
				/>
			</div>
			<p class="-mt-3 text-p-xs text-ink-gray-5">
				Use a period of no more than 366 calendar days. The end date cannot be after today
				on this site.
			</p>
			<ErrorMessage v-if="shownErrors.from_date" :message="shownErrors.from_date" />
			<ErrorMessage v-if="shownErrors.to_date" :message="shownErrors.to_date" />

			<FormControl
				type="date"
				label="Physical-count history from"
				:modelValue="form.count_history_from"
				@update:modelValue="(v) => (form.count_history_from = v)"
			/>
			<p class="-mt-3 text-p-xs text-ink-gray-5">
				This date controls the count-history evidence. It is not a stock posting date.
			</p>
			<ErrorMessage
				v-if="shownErrors.count_history_from"
				:message="shownErrors.count_history_from"
			/>

			<div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
				<FormControl
					type="number"
					label="Items per warehouse"
					description="Choose a whole number from 1 to 40."
					min="1"
					max="40"
					step="1"
					:modelValue="form.line_limit"
					@update:modelValue="(v) => (form.line_limit = v)"
				/>
				<FormControl
					type="number"
					label="Recount interval (days)"
					description="Choose a whole number from 1 to 365."
					min="1"
					max="365"
					step="1"
					:modelValue="form.recount_after_days"
					@update:modelValue="(v) => (form.recount_after_days = v)"
				/>
			</div>
			<ErrorMessage v-if="shownErrors.line_limit" :message="shownErrors.line_limit" />
			<ErrorMessage
				v-if="shownErrors.recount_after_days"
				:message="shownErrors.recount_after_days"
			/>

			<FormControl
				type="select"
				label="Evidence basis"
				:options="BASIS_OPTIONS"
				:modelValue="form.basis"
				@update:modelValue="(v) => (form.basis = v)"
			/>
			<ErrorMessage v-if="shownErrors.basis" :message="shownErrors.basis" />

			<FormControl
				type="select"
				label="Review scope"
				:options="SCOPE_OPTIONS"
				:modelValue="form.review_scope"
				@update:modelValue="(v) => (form.review_scope = v)"
			/>
			<ErrorMessage v-if="shownErrors.review_scope" :message="shownErrors.review_scope" />

			<FormControl
				type="text"
				label="Count-plan policy reference"
				description="This identifies your saved policy. It is not proof of policy approval."
				:modelValue="form.policy_version"
				@update:modelValue="(v) => (form.policy_version = v)"
			/>
			<ErrorMessage
				v-if="shownErrors.policy_version"
				:message="shownErrors.policy_version"
			/>
		</div>

		<div class="mt-4">
			<Button label="Save configuration" :loading="saving" @click="save" />
			<p class="mt-2 text-p-xs text-ink-gray-5">
				You can save unfinished setup. Run Now stays unavailable until all required
				settings are valid and saved.
			</p>
		</div>
	</fieldset>
</template>

<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { Autocomplete, Button, ErrorMessage, FormControl } from "frappe-ui";
import { searchLink } from "@/api";
import { getCycleCountReviewDefaults } from "@/api/agents";
import { useLinkSearch } from "@/composables/useLinkSearch";
import WarehousePicker from "@/pages/agents/WarehousePicker.vue";

const BASIS_OPTIONS = [{ label: "Current records only", value: "current_records" }];
const SCOPE_OPTIONS = [
	{ label: "Count plan only: approval does not change stock", value: "count_plan_only" },
];
const FORM_KEYS = [
	"company",
	"from_date",
	"to_date",
	"count_history_from",
	"warehouses",
	"line_limit",
	"recount_after_days",
	"basis",
	"review_scope",
	"policy_version",
];

const props = defineProps({
	config: { type: Object, default: () => ({}) },
	validationErrors: { type: Object, default: () => ({}) },
	saving: { type: Boolean, default: false },
});
const emit = defineEmits(["save", "dirty"]);

const form = reactive({
	company: "",
	from_date: "",
	to_date: "",
	count_history_from: "",
	warehouses: [],
	line_limit: "",
	recount_after_days: "",
	basis: "",
	review_scope: "",
	policy_version: "",
});
const initialState = ref("");
const defaults = ref(null);
const defaultsLoading = ref(false);
const defaultsMessage = ref("");
let defaultsRequest = 0;

const companySearch = useLinkSearch((query) => searchLink("Company", query), {
	mapper: (rows) => rows.map((row) => ({ label: row.value, value: row.value })),
});
const companyValue = computed(() =>
	form.company ? { label: form.company, value: form.company } : null
);
const warehouseOptions = computed(() => defaults.value?.warehouse_options || []);

function state() {
	return JSON.stringify(
		Object.fromEntries(
			FORM_KEYS.map((key) => [key, Array.isArray(form[key]) ? [...form[key]] : form[key]])
		)
	);
}

function seed(config) {
	const source = config || {};
	for (const key of FORM_KEYS) {
		if (key === "warehouses") {
			form.warehouses = Array.isArray(source.warehouses)
				? source.warehouses.filter((value) => typeof value === "string" && value)
				: [];
		} else {
			const value = source[key];
			form[key] = value == null ? "" : String(value);
		}
	}
	initialState.value = state();
}

// Server issues describe the last saved state. Leave an unchanged issue visible, but do not
// keep an old issue beside a field the user has already changed.
const shownErrors = computed(() =>
	Object.fromEntries(
		Object.entries(props.validationErrors || {}).filter(([key]) => {
			if (!FORM_KEYS.includes(key)) return true;
			const saved = props.config?.[key];
			if (key === "warehouses")
				return (
					JSON.stringify(form[key]) === JSON.stringify(Array.isArray(saved) ? saved : [])
				);
			return String(form[key]) === String(saved == null ? "" : saved);
		})
	)
);

watch(
	() => props.config,
	(value) => seed(value),
	{ immediate: true, deep: true }
);
watch(form, () => emit("dirty", state() !== initialState.value), { deep: true, immediate: true });

function onCompanyPick(option) {
	const company = option?.value ? String(option.value) : "";
	if (company === form.company) return;
	form.company = company;
	form.warehouses = [];
	defaults.value = null;
	defaultsMessage.value = "";
}

async function loadDefaults() {
	const request = ++defaultsRequest;
	defaults.value = null;
	defaultsMessage.value = "";
	if (props.saving) return;
	defaultsLoading.value = true;
	try {
		const data = await getCycleCountReviewDefaults(form.company || undefined);
		if (request !== defaultsRequest) return;
		defaults.value = data || null;
		defaultsMessage.value = data?.message || "";
	} catch {
		if (request === defaultsRequest)
			defaultsMessage.value =
				"Could not load suggested settings. Check company read access, then retry or enter the settings manually.";
	} finally {
		if (request === defaultsRequest) defaultsLoading.value = false;
	}
}

async function fillEmptySettings() {
	if (
		!defaults.value?.defaults ||
		(form.company && defaults.value.defaults.company !== form.company)
	)
		await loadDefaults();
	const suggested = defaults.value?.defaults;
	if (!suggested || (form.company && suggested.company !== form.company)) return;
	for (const [key, value] of Object.entries(suggested)) {
		if (FORM_KEYS.includes(key) && key !== "warehouses" && form[key] === "")
			form[key] = String(value);
	}
}

watch(
	() => form.company,
	() => loadDefaults(),
	{ immediate: true }
);
onBeforeUnmount(() => {
	defaultsRequest += 1;
	companySearch.cleanup();
});

function savedNumber(value) {
	const text = String(value ?? "").trim();
	if (!text) return undefined;
	const number = Number(text);
	return Number.isFinite(number) ? number : text;
}

function save() {
	const next = {};
	for (const key of [
		"company",
		"from_date",
		"to_date",
		"count_history_from",
		"basis",
		"review_scope",
		"policy_version",
	]) {
		const value = String(form[key] ?? "").trim();
		if (value) next[key] = value;
	}
	if (form.warehouses.length) next.warehouses = [...new Set(form.warehouses)];
	for (const key of ["line_limit", "recount_after_days"]) {
		const value = savedNumber(form[key]);
		if (value !== undefined) next[key] = value;
	}
	emit("save", next);
}
</script>
