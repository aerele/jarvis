<template>
	<div>
		<p class="text-sm text-ink-gray-6">
			Settings this agent reads when it runs. Leave a field empty to use the default.
		</p>

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
					v-else-if="f.type === 'select'"
					type="select"
					:label="f.label"
					:options="f.options"
					:description="f.help"
					:modelValue="form[f.key]"
					@update:modelValue="(v) => (form[f.key] = v)"
				/>

				<FormControl
					v-else-if="f.type === 'number'"
					type="number"
					:label="f.label"
					:description="f.help"
					:modelValue="form[f.key]"
					@update:modelValue="(v) => (form[f.key] = v)"
				>
					<template v-if="f.suffix" #suffix>
						<span class="text-ink-gray-5">{{ f.suffix }}</span>
					</template>
				</FormControl>
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
		</div>
	</div>
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
import { computed, reactive, ref, watch } from "vue";
import { Autocomplete, Button, ErrorMessage, FormControl } from "frappe-ui";
import DocSection from "@/components/doc/DocSection.vue";
import { searchLink } from "@/api";
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
const visibleFields = computed(() => [...SCOPE_CONFIG_FIELDS, ...agentSpecificFields.value]);
// jarvis#1063: the key set for THIS agent's rendered fields - not the global
// CONFIG_FIELD_SET. A key this agent's config_keys does not name (e.g. a
// stale benchmark_value on a non-close-auditor installation, saved back when
// every field always rendered) has no control here and must fall through to
// Advanced (JSON) - seed()/save() key off this.
const visibleKeys = computed(() => new Set(visibleFields.value.flatMap((f) => f.keys || [f.key])));

const emit = defineEmits(["save"]);

// One string per known key - every control (link/date/number/select) binds
// here as plain text; numbers are validated/coerced only on save.
const form = reactive({
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
}

watch([() => props.config, visibleKeys], () => seed(props.config), { immediate: true });

function onAdvancedInput(v) {
	advanced.value = v;
	advancedError.value = "";
}

// ── Company / Fiscal year: debounced + fenced Link search, primed on open ───
// (mirrors AgentAccessEditor.vue's people picker and FilterValueControl.vue's
// generic Link control - 300ms debounce, a monotonic sequence so a slow early
// response can never overwrite a newer one, and the first page loads on
// focus/click so the menu is never empty before a keystroke.)
function makeLinkField(doctype) {
	const options = ref([]);
	const primed = ref(false);
	let timer = null;
	let seq = 0;

	async function runSearch(q) {
		const mySeq = ++seq;
		try {
			const rows = (await searchLink(doctype, q || "")) || [];
			if (mySeq === seq)
				options.value = rows.map((r) => ({ label: r.value, value: r.value }));
		} catch (e) {
			if (mySeq === seq) options.value = [];
		}
	}
	function prime() {
		if (primed.value) return;
		primed.value = true;
		runSearch("");
	}
	function onQuery(q) {
		primed.value = true;
		clearTimeout(timer);
		timer = setTimeout(() => runSearch(q), 300);
	}
	return { options, prime, onQuery };
}

const linkFields = {
	company: makeLinkField("Company"),
	fiscal_year: makeLinkField("Fiscal Year"),
};
function linkValue(key) {
	return form[key] ? { label: form[key], value: form[key] } : null;
}
function onLinkPick(key, opt) {
	form[key] = opt && opt.value ? String(opt.value) : "";
}

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
	emit("save", merged);
}
</script>
