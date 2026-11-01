<template>
	<fieldset :disabled="saving" class="min-w-0">
		<p class="text-sm text-ink-gray-6">
			Choose one company and one Warehouse group or leaf. The audit reads submitted physical
			counts in that Warehouse tree. It does not post a stock adjustment.
		</p>

		<div
			v-if="Object.keys(shownErrors).length"
			class="mt-3 rounded-lg border border-outline-gray-2 bg-surface-gray-1 p-3 text-sm text-ink-gray-7"
			role="status"
		>
			<p class="font-medium">Complete these settings before saving</p>
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
					Select the company that owns the submitted physical-count records.
				</p>
			</div>

			<div>
				<label class="mb-1.5 block text-xs text-ink-gray-5">Warehouse scope</label>
				<div @focusin="warehouseSearch.prime()" @click="warehouseSearch.prime()">
					<Autocomplete
						:options="warehouseSearch.options.value"
						:modelValue="warehouseValue"
						:disabled="!form.company"
						placeholder="Search Warehouse group or leaf…"
						@update:query="warehouseSearch.onQuery"
						@update:modelValue="onWarehousePick"
					/>
				</div>
				<p class="mt-1.5 text-p-xs text-ink-gray-5">
					You can select an enabled group or leaf. A group includes its enabled leaf
					warehouses.
				</p>
			</div>

			<div>
				<label class="mb-1.5 block text-xs text-ink-gray-5">Stock reviewer</label>
				<div @focusin="reviewerSearch.prime()" @click="reviewerSearch.prime()">
					<Autocomplete
						:options="reviewerSearch.options.value"
						:modelValue="reviewerValue"
						placeholder="Search enabled user…"
						@update:query="reviewerSearch.onQuery"
						@update:modelValue="onReviewerPick"
					/>
				</div>
				<p class="mt-1.5 text-p-xs text-ink-gray-5">
					The evaluator records this named reviewer in its worklist.
				</p>
			</div>

			<FormControl
				type="textarea"
				label="Test notice (optional)"
				description="Use plain text only. It appears with the test result."
				:modelValue="form.test_declaration"
				@update:modelValue="(value) => (form.test_declaration = value)"
			/>
		</div>

		<div class="mt-4">
			<Button label="Save configuration" :loading="saving" @click="save" />
			<p class="mt-2 text-p-xs text-ink-gray-5">
				Jarvis checks the selected company, Warehouse root, and reviewer again before it
				enables or runs the audit.
			</p>
		</div>
	</fieldset>
</template>

<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { Autocomplete, Button, FormControl } from "frappe-ui";
import { searchLink } from "@/api";
import { useLinkSearch } from "@/composables/useLinkSearch";

const PASSIVE_SCOPE_KEYS = [
	"fiscal_year",
	"from_date",
	"to_date",
	"prior_period_from",
	"prior_period_to",
];

const props = defineProps({
	config: { type: Object, default: () => ({}) },
	validationErrors: { type: Object, default: () => ({}) },
	saving: { type: Boolean, default: false },
});
const emit = defineEmits(["save", "dirty"]);

const form = reactive({
	company: "",
	warehouse: "",
	reviewer: "",
	test_declaration: "",
});
const passiveScope = ref({});
const initialState = ref("");
const localErrors = ref({});

const companySearch = useLinkSearch((query) => searchLink("Company", query), {
	mapper: (rows) => rows.map((row) => ({ label: row.value, value: row.value })),
});
const warehouseSearch = useLinkSearch(
	(query) => {
		if (!form.company) return Promise.resolve([]);
		return searchLink("Warehouse", query, 20, undefined, undefined, [
			["Warehouse", "company", "=", form.company],
			["Warehouse", "disabled", "=", 0],
		]);
	},
	{ mapper: (rows) => rows.map((row) => ({ label: row.value, value: row.value })) }
);
const reviewerSearch = useLinkSearch(
	(query) => searchLink("User", query, 20, undefined, undefined, [["User", "enabled", "=", 1]]),
	{ mapper: (rows) => rows.map((row) => ({ label: row.value, value: row.value })) }
);

const companyValue = computed(() =>
	form.company ? { label: form.company, value: form.company } : null
);
const warehouseValue = computed(() =>
	form.warehouse ? { label: form.warehouse, value: form.warehouse } : null
);
const reviewerValue = computed(() =>
	form.reviewer ? { label: form.reviewer, value: form.reviewer } : null
);
const shownErrors = computed(() => ({ ...props.validationErrors, ...localErrors.value }));

function state() {
	return JSON.stringify({ ...form, passiveScope: passiveScope.value });
}

function seed(config) {
	const source = config || {};
	const record = source.record_scope;
	const settings = source.stock_variance;
	form.company = typeof source.company === "string" ? source.company : "";
	form.warehouse =
		record && record.doctype === "Warehouse" && typeof record.name === "string"
			? record.name
			: "";
	form.reviewer = settings && typeof settings.reviewer === "string" ? settings.reviewer : "";
	form.test_declaration =
		settings && typeof settings.test_declaration === "string" ? settings.test_declaration : "";
	passiveScope.value = Object.fromEntries(
		PASSIVE_SCOPE_KEYS.filter((key) => typeof source[key] === "string" && source[key]).map(
			(key) => [key, source[key]]
		)
	);
	localErrors.value = {};
	initialState.value = state();
}

watch(
	() => props.config,
	(value) => seed(value),
	{ immediate: true, deep: true }
);
watch(form, () => emit("dirty", state() !== initialState.value), { deep: true, immediate: true });

function clearError(...keys) {
	const next = { ...localErrors.value };
	for (const key of keys) delete next[key];
	localErrors.value = next;
}

function onCompanyPick(option) {
	const company = option?.value ? String(option.value) : "";
	if (company === form.company) return;
	form.company = company;
	form.warehouse = "";
	warehouseSearch.reprime();
	clearError("company", "record_scope");
}

function onWarehousePick(option) {
	form.warehouse = option?.value ? String(option.value) : "";
	clearError("record_scope");
}

function onReviewerPick(option) {
	form.reviewer = option?.value ? String(option.value) : "";
	clearError("stock_variance.reviewer", "stock_variance");
}

function save() {
	const errors = {};
	if (!form.company.trim()) errors.company = "Select a company.";
	if (!form.warehouse.trim()) errors.record_scope = "Select an enabled Warehouse group or leaf.";
	if (!form.reviewer.trim())
		errors["stock_variance.reviewer"] = "Select an enabled stock reviewer.";
	if (form.test_declaration.length > 500)
		errors["stock_variance.test_declaration"] =
			"Keep the test notice to 500 characters or less.";
	if (Object.keys(errors).length) {
		localErrors.value = errors;
		return;
	}
	const stockVariance = { reviewer: form.reviewer.trim() };
	if (form.test_declaration) stockVariance.test_declaration = form.test_declaration;
	emit("save", {
		...passiveScope.value,
		company: form.company.trim(),
		record_scope: { doctype: "Warehouse", name: form.warehouse.trim() },
		stock_variance: stockVariance,
	});
}

onBeforeUnmount(() => {
	companySearch.cleanup();
	warehouseSearch.cleanup();
	reviewerSearch.cleanup();
});
</script>
