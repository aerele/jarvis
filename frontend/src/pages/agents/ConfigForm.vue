<template>
	<fieldset :disabled="operatorReview && saving" class="min-w-0">
		<p class="text-sm text-ink-gray-6">
			{{
				operatorReview
					? bankRecon
						? "Bank reconciliation review reads one company bank account for the statement window below and proposes matches for a named reviewer. Manual runs only; nothing is reconciled, allocated or posted."
						: arReview
						? "AR review includes older open receivables, uses the evidence cutoff and prepares unsent reminder text only. Manual runs; no sending, Dunning creation or accounting changes."
						: "AP document review uses the company, fiscal period, evidence cutoff and matching settings below. Manual runs only. Preview creates no review requests; live review never submits or pays an invoice."
					: "Settings this agent reads when it runs. Leave a field empty to use the default."
			}}
		</p>
		<div
			v-if="operatorReview"
			class="mt-3 rounded-lg bg-surface-gray-1 p-3 text-sm text-ink-gray-6"
		>
			<p v-if="bankRecon">
				Starter settings: the previous calendar month, a 30-calendar-day voucher lookback
				and proposals only. Choose the bank account with your named reviewer and save.
			</p>
			<p v-else-if="arReview">
				Starter settings: a 3-calendar-day settlement hold and current records only. The
				fiscal year provides review context; older open invoices remain included.
				India-specific context is detected from India Compliance and the company country.
				No accounting framework, tax compliance or policy approval is inferred.
			</p>
			<p v-else>
				Starter settings: 0% net-price tolerance, current records and document matching
				only. Dates come from ERPNext fiscal years and this site's date. These are
				application defaults, not accounting-standard certification or policy approval.
				Existing values are preserved; review the settings with your named reviewer and
				save.
			</p>
			<Button
				class="mt-2"
				label="Fill empty settings"
				:loading="defaultsLoading"
				@click="loadDefaults"
			/>
			<p v-if="defaultsMessage" class="mt-2">{{ defaultsMessage }}</p>
		</div>
		<div
			v-if="operatorReview && Object.keys(shownErrors).length"
			class="mt-3 rounded-lg border border-outline-gray-2 bg-surface-gray-1 p-3 text-sm text-ink-gray-7"
			role="status"
		>
			<p class="font-medium">Complete these settings before running</p>
			<ul class="mt-2 list-disc space-y-1 pl-5">
				<li v-for="(message, key) in shownErrors" :key="key">{{ message }}</li>
			</ul>
			<p class="mt-2">Fill the fields below, then save to check the configuration.</p>
		</div>

		<div class="mt-4 space-y-5">
			<template v-for="f in visibleFields" :key="f.key || f.keys.join('-')">
				<!-- Company / Fiscal year: frappe-ui Autocomplete fed by
				     frappe.desk.search.search_link - the same whitelisted, generic
				     Link-picker call every other Link field in this app uses
				     (FilterValueControl.vue, TriggerDetail.vue). There is no single
				     reusable <LinkField> component to import; this is that same
				     small pattern, applied twice. -->
				<div v-if="f.type === 'link'">
					<label class="mb-1.5 block text-xs text-ink-gray-5">{{ f.label }}</label>
					<div @focusin="linkFields[f.key].prime()" @click="linkFields[f.key].prime()">
						<Autocomplete
							:options="linkFields[f.key].options.value"
							:modelValue="linkValue(f.key)"
							:placeholder="`Search ${f.linkDoctype}…`"
							@update:query="(q) => linkFields[f.key].onQuery(q)"
							@update:modelValue="(opt) => onLinkPick(f.key, opt)"
						/>
					</div>
					<p class="mt-1.5 text-p-xs text-ink-gray-5">{{ f.help }}</p>
				</div>

				<div v-else-if="f.type === 'date-range'">
					<div class="grid grid-cols-2 gap-4">
						<FormControl
							type="date"
							:label="f.labels[0]"
							:modelValue="form[f.keys[0]]"
							@update:modelValue="(v) => (form[f.keys[0]] = v)"
						/>
						<FormControl
							type="date"
							:label="f.labels[1]"
							:modelValue="form[f.keys[1]]"
							@update:modelValue="(v) => (form[f.keys[1]] = v)"
						/>
					</div>
					<p class="mt-1.5 text-p-xs text-ink-gray-5">{{ f.help }}</p>
				</div>

				<FormControl
					v-else-if="['text', 'date'].includes(f.type)"
					:type="f.type"
					:label="f.label"
					:description="f.help"
					:modelValue="form[f.key]"
					@update:modelValue="(value) => (form[f.key] = value)"
				/>

				<FormControl
					v-else-if="f.type === 'select'"
					type="select"
					:label="f.label"
					:options="f.options"
					:description="f.help"
					:modelValue="form[f.key]"
					@update:modelValue="(value) => onSelect(f.key, value)"
				/>

				<FormControl
					v-else-if="f.type === 'number'"
					type="number"
					:label="f.label"
					:description="f.help"
					:placeholder="f.placeholder"
					:modelValue="form[f.key]"
					@update:modelValue="(v) => (form[f.key] = v)"
				>
					<template v-if="f.suffix" #suffix>
						<span class="text-ink-gray-5">{{ f.suffix }}</span>
					</template>
				</FormControl>
				<ErrorMessage
					v-for="key in (f.keys || [f.key]).filter((field) => shownErrors[field])"
					:key="key"
					:message="shownErrors[key]"
				/>
			</template>

			<p v-if="!agentSpecificFields.length" class="text-p-xs text-ink-gray-5">
				This agent has no additional settings.
			</p>
		</div>

		<!-- §14 F3: any key outside CONFIG_FIELD_SET (unknown, or an array/object
		     value) lives here only. -->
		<DocSection label="Advanced (JSON)" :opened="false" class="mt-4">
			<FormControl
				type="textarea"
				class="font-mono"
				:rows="6"
				:modelValue="advanced"
				@update:modelValue="onAdvancedInput"
			/>
			<div class="mt-1 text-xs text-ink-gray-5">
				Enter values as JSON, for example {"company": "Acme Ltd", "materiality":
				{"pl_balance": 50000}}. Form fields above win on matching keys - a value this page
				renders a field for (e.g. "materiality": {"benchmark_value": ...}) belongs in that
				field, not here.
			</div>
			<ErrorMessage class="mt-2" :message="advancedError" />
		</DocSection>

		<div class="mt-4">
			<Button label="Save configuration" :loading="saving" @click="save" />
			<p v-if="operatorReview" class="mt-2 text-p-xs text-ink-gray-5">
				You can save unfinished setup. Run Now stays unavailable until all required
				settings are valid and saved. Suggestions are not applied to a run until you save.
			</p>
		</div>
	</fieldset>
</template>

<script setup>
// ConfigForm - jarvis#1062 owner feedback: the known engagement settings
// (CONFIG_FIELD_SET, @/lib/agentConfigFields) get real, purpose-built fields -
// link pickers, dates, numbers, a select - ALWAYS rendered, not a code-key
// reference list. Everything else (an unrecognised key, or any array/object
// value) lives in the collapsed "Advanced (JSON)" editor (mono textarea,
// JSON.parse-validated). Save merges the known fields OVER the advanced JSON
// (form fields win on matching keys) and emits the merged object; the parent
// persists via setAgentConfig. An empty field means the key is ABSENT from
// the saved config, not saved as "" - that is what "leave it to use the
// default" means server-side.
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { Autocomplete, Button, ErrorMessage, FormControl } from "frappe-ui";
import DocSection from "@/components/doc/DocSection.vue";
import { searchLink } from "@/api";
import {
	getAPReviewDefaults,
	getARReviewDefaults,
	getBankReconReviewDefaults,
} from "@/api/agents";
import { useLinkSearch } from "@/composables/useLinkSearch";
import {
	SCOPE_CONFIG_FIELDS,
	AGENT_SPECIFIC_CONFIG_FIELDS,
	CONFIG_FIELD_LABELS,
	NUMBER_CONFIG_KEYS,
	KEY_TO_PATH,
	getPath,
	setPath,
	deletePath,
} from "@/lib/agentConfigFields";

const props = defineProps({
	config: { type: Object, default: () => ({}) }, // parsed installation config
	// jarvis#1063 (jarvis-only half): the current agent's Jarvis Agent Listing
	// config_keys (get_agent), already parsed to a plain array by the parent.
	// Gates which AGENT_SPECIFIC_CONFIG_FIELDS render - the scope fields
	// (SCOPE_CONFIG_FIELDS) always render regardless.
	configKeys: { type: Array, default: () => [] },
	validationErrors: { type: Object, default: () => ({}) },
	saving: { type: Boolean, default: false },
});

// The agent-specific fields this agent's config_keys actually names, in
// CONFIG_FIELD_SET's display order.
const agentSpecificFields = computed(() =>
	AGENT_SPECIFIC_CONFIG_FIELDS.filter((f) =>
		(f.paths || [f.path]).some((p) => props.configKeys.includes(p))
	)
);
// Scope fields first (always), then whichever agent-specific fields apply -
// the single list the template's v-for renders.
const operatorReview = computed(() => props.configKeys.includes("review_scope"));
const arReview = computed(() => props.configKeys.includes("settlement_hold_days"));
const bankRecon = computed(() => props.configKeys.includes("voucher_lookback_days"));
const defaultsData = ref(null);
const defaultsLoading = ref(false);
const defaultsMessage = ref("");
let defaultsRequest = 0;
const visibleFields = computed(() => [
	...SCOPE_CONFIG_FIELDS.map((field) => {
		if (!operatorReview.value || field.key === "fiscal_year") return field;
		if (bankRecon.value && field.type === "date-range")
			return { ...field, help: "Statement window. Lines dated inside it are reviewed." };
		return {
			...field,
			help:
				field.key === "company"
					? "Suggested from your readable default company; confirm the company before saving."
					: arReview.value
					? "Fiscal-year review context only: all older open receivables remain included through the evidence cutoff. Choosing a year resets dates through today or year end."
					: "Choosing a fiscal year resets this posting-date window through today or year end. Dates remain editable; clear Fiscal year for a cross-year period. Linked sources may predate it.",
		};
	}),
	...agentSpecificFields.value.map((field) => {
		if (bankRecon.value) {
			if (field.key === "policy_version")
				return {
					...field,
					label: "Bank reconciliation policy reference",
					help: "BANK-RECON-v1 identifies the saved starter settings, not evidence of policy approval.",
				};
			if (field.key === "review_scope")
				return {
					...field,
					options: [
						{ label: "Proposals only, no reconciling", value: "proposals_only" },
					],
				};
			return field;
		}
		if (!arReview.value) return field;
		if (field.key === "policy_version")
			return {
				...field,
				label: "AR review policy reference",
				help: "AR-REVIEW-v1 identifies the saved starter settings, not an accounting standard or evidence of policy approval.",
			};
		if (field.key === "review_scope")
			return {
				...field,
				options: [
					{
						label: "Review and drafts only — no sending",
						value: "review_and_drafts_only",
					},
				],
			};
		return field;
	}),
]);
// jarvis#1063: the key set for THIS agent's rendered fields - not the global
// CONFIG_FIELD_SET. A key this agent's config_keys does not name (e.g. a
// stale benchmark_value on a non-close-auditor installation, saved back when
// every field always rendered) has no control here and must fall through to
// Advanced (JSON) - seed()/save() key off this.
const visibleKeys = computed(() => new Set(visibleFields.value.flatMap((f) => f.keys || [f.key])));

// Server issues describe the SAVED config. Hide a field's issue once its value
// differs from what is saved (a filled default or an edit) until the next save
// re-checks it; issues without a form field always show.
const shownErrors = computed(() =>
	Object.fromEntries(
		Object.entries(props.validationErrors || {}).filter(([key]) => {
			if (!(key in form)) return true;
			const saved = getPath(props.config || {}, KEY_TO_PATH[key] || key);
			return String(form[key]) === String(saved == null ? "" : saved);
		})
	)
);

const emit = defineEmits(["save", "dirty"]);

// One string per known key - every control (link/date/number/select) binds
// here as plain text; numbers are validated/coerced only on save.
const form = reactive({
	report_date: "",
	policy_version: "",
	bank_account: "",
	voucher_lookback_days: "",
	price_tolerance_percent: "",
	settlement_hold_days: "",
	basis: "",
	review_scope: "",
	company: "",
	fiscal_year: "",
	from_date: "",
	to_date: "",
	benchmark_value: "",
	percentage: "",
	engagement_risk_level: "",
	rounding_step: "",
});
const advanced = ref("{}");
const advancedError = ref("");
const initialState = ref("");

function formState() {
	return JSON.stringify({ form, advanced: advanced.value });
}

function seed(cfg) {
	const c = cfg || {};
	for (const key of Object.keys(form)) {
		if (!visibleKeys.value.has(key)) {
			// no rendered control here - leave it out of `form` entirely so
			// save() (which only reads visibleKeys) never touches it; it stays
			// visible and editable in Advanced (JSON) below instead of silently
			// vanishing.
			form[key] = "";
			continue;
		}
		const path = KEY_TO_PATH[key] || key;
		// jarvis#1063 CRITICAL fix: the nested path (e.g.
		// "materiality.benchmark_value") is the source of truth. A flat
		// top-level key of the same name (pre-nesting saves) is a MIGRATION
		// fallback, read only when the nested value is absent - never the
		// other way round.
		let v = getPath(c, path);
		if (v == null && path !== key) v = c[key];
		form[key] = v == null ? "" : String(v);
	}
	// Advanced (JSON) holds everything NOT covered by a visible field's path -
	// a deep clone with each visible path (and its flat legacy counterpart,
	// now migrated into `form` above) removed. deletePath also drops a
	// now-empty intermediate object (e.g. `materiality`), so an installation
	// whose only materiality keys are all rendered here does not leave a
	// stray `{"materiality": {}}` in Advanced.
	const rest = JSON.parse(JSON.stringify(c));
	for (const key of visibleKeys.value) {
		const path = KEY_TO_PATH[key] || key;
		deletePath(rest, path);
		if (path !== key) delete rest[key];
	}
	advanced.value = JSON.stringify(rest, null, 2);
	advancedError.value = "";
	initialState.value = formState();
}

watch([() => props.config, () => [...visibleKeys.value].join(",")], () => seed(props.config), {
	immediate: true,
});
watch([form, advanced, initialState], () => emit("dirty", formState() !== initialState.value), {
	deep: true,
	immediate: true,
});

function onAdvancedInput(v) {
	advanced.value = v;
	advancedError.value = "";
}

// ── Company / Fiscal year: debounced + fenced Link search, primed on open ───
// (mirrors AgentAccessEditor.vue's people picker and FilterValueControl.vue's
// generic Link control - all three now share useLinkSearch, jarvis#1062.)
function makeLinkField(doctype) {
	return useLinkSearch(
		(query) => {
			if (operatorReview.value && doctype === "Fiscal Year") {
				const data = defaultsData.value;
				if (data?.defaults.company !== form.company) return [];
				return data.fiscal_years
					.filter((fiscal) =>
						fiscal.name.toLowerCase().includes(query.trim().toLowerCase())
					)
					.map((fiscal) => ({ value: fiscal.name }));
			}
			if (bankRecon.value && doctype === "Bank Account") {
				const data = defaultsData.value;
				if (data?.defaults.company !== form.company) return [];
				return (data.bank_accounts || [])
					.filter((row) =>
						`${row.name} ${row.account || ""}`
							.toLowerCase()
							.includes(query.trim().toLowerCase())
					)
					.map((row) => ({ value: row.name }));
			}
			return searchLink(doctype, query);
		},
		{ mapper: (rows) => rows.map((row) => ({ label: row.value, value: row.value })) }
	);
}

const linkFields = {
	company: makeLinkField("Company"),
	fiscal_year: makeLinkField("Fiscal Year"),
	bank_account: makeLinkField("Bank Account"),
};
watch([operatorReview, defaultsData], () => {
	linkFields.fiscal_year.reprime();
	linkFields.bank_account.reprime();
	if (operatorReview.value && defaultsData.value) linkFields.fiscal_year.prime();
});
function linkValue(key) {
	return form[key] ? { label: form[key], value: form[key] } : null;
}
function onLinkPick(key, opt) {
	const value = opt && opt.value ? String(opt.value) : "";
	if (form[key] === value) return;
	if (operatorReview.value && key === "company") {
		form.fiscal_year = "";
		form.bank_account = "";
	}
	onSelect(key, value);
}

function fiscalDates(fiscal) {
	if (!fiscal || !defaultsData.value?.site_date) return null;
	const cutoff = [fiscal.to_date, defaultsData.value.site_date].sort()[0];
	if (cutoff < fiscal.from_date) return null;
	return { from_date: fiscal.from_date, to_date: cutoff, report_date: cutoff };
}

function onSelect(key, value) {
	form[key] = value;
	if (
		operatorReview.value &&
		key === "fiscal_year" &&
		value &&
		defaultsData.value?.defaults.company === form.company
	) {
		const fiscal = defaultsData.value?.fiscal_years.find((entry) => entry.name === value);
		const dates = fiscalDates(fiscal);
		if (dates) {
			Object.assign(form, dates);
			defaultsMessage.value = "";
		} else
			defaultsMessage.value =
				"This fiscal year is unavailable or has not started. Enter a period on or before today's site date.";
	}
}

function fillEmptySettings() {
	const data = defaultsData.value;
	if (!data || (form.company && data.defaults.company !== form.company)) return;
	for (const [key, value] of Object.entries(data.defaults)) {
		if (visibleKeys.value.has(key) && form[key] === "") form[key] = String(value);
	}
	const anchor = form.from_date || form.to_date || form.report_date || data.site_date;
	const candidates = data.fiscal_years.filter((fiscal) => {
		if (form.fiscal_year) return fiscal.name === form.fiscal_year;
		return [anchor, form.from_date, form.to_date]
			.filter(Boolean)
			.every((value) => fiscal.from_date <= value && value <= fiscal.to_date);
	});
	const fiscal = candidates.length === 1 ? candidates[0] : null;
	const dates = fiscalDates(fiscal);
	if (dates) {
		if (!form.fiscal_year) form.fiscal_year = fiscal.name;
		if (!form.from_date) form.from_date = dates.from_date;
		if (!form.to_date)
			form.to_date = [dates.to_date, form.report_date || data.site_date].sort()[0];
	}
	if (!form.report_date)
		form.report_date = [form.to_date || data.site_date, data.site_date].sort()[0];
}

async function loadDefaults() {
	const request = ++defaultsRequest;
	defaultsData.value = null;
	defaultsMessage.value = "";
	defaultsLoading.value = false;
	if (!operatorReview.value || props.saving) return;
	const state = formState();
	defaultsLoading.value = true;
	try {
		const data = await (bankRecon.value
			? getBankReconReviewDefaults
			: arReview.value
			? getARReviewDefaults
			: getAPReviewDefaults)(form.company || undefined);
		if (request !== defaultsRequest) return;
		defaultsData.value = data;
		defaultsMessage.value = data.message || "";
		if (!props.saving && state === formState()) fillEmptySettings();
	} catch {
		if (request === defaultsRequest) {
			defaultsMessage.value =
				"Could not load suggested settings. Check company and fiscal-year read access, then retry, or enter settings manually.";
		}
	} finally {
		if (request === defaultsRequest) defaultsLoading.value = false;
	}
}

watch([() => props.config, operatorReview, () => form.company, () => props.saving], loadDefaults, {
	immediate: true,
});
onBeforeUnmount(() => {
	defaultsRequest += 1;
	for (const link of Object.values(linkFields)) link.cleanup();
});

function save() {
	// 1) advanced JSON must parse to an object
	let base = {};
	const raw = advanced.value.trim();
	if (raw) {
		try {
			base = JSON.parse(raw);
		} catch (e) {
			advancedError.value = "Advanced JSON is not valid: " + e.message;
			return;
		}
		if (!base || typeof base !== "object" || Array.isArray(base)) {
			advancedError.value = "Advanced JSON must be an object ({...}).";
			return;
		}
	}
	// 2) the known fields merge OVER the JSON, WRITTEN THROUGH EACH FIELD'S
	// PATH (jarvis#1063 CRITICAL fix - a flat save of a key the bundle reads
	// nested, e.g. close-auditor's materiality.*, never reaches the
	// evaluator). setPath merges into an existing object at that path rather
	// than replacing it (an existing `materiality.pl_balance` from Advanced
	// survives a benchmark_value edit); an empty field deletes its path
	// rather than saving "" (§14 F3 / round-2 parity), pruning `materiality`
	// entirely once every field under it is cleared. Either way, the
	// migration is one-directional: a flat legacy key of the same name is
	// always removed, since the nested path is now the source of truth.
	const merged = JSON.parse(JSON.stringify(base));
	for (const key of visibleKeys.value) {
		const path = KEY_TO_PATH[key] || key;
		const v = String(form[key] ?? "").trim();
		if (path !== key) delete merged[key];
		if (v === "") {
			deletePath(merged, path);
			continue;
		}
		if (NUMBER_CONFIG_KEYS.includes(key)) {
			const n = Number(v);
			if (isNaN(n)) {
				advancedError.value = `"${CONFIG_FIELD_LABELS[key]}" must be a number.`;
				return;
			}
			setPath(merged, path, n);
		} else {
			setPath(merged, path, v);
		}
	}
	advancedError.value = "";
	defaultsRequest += 1;
	defaultsLoading.value = false;
	emit("save", merged);
}
</script>
