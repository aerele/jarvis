<template>
	<div>
		<p class="text-sm text-ink-gray-6">
			Settings this agent reads when it runs. Leave a field empty to use the default.
		</p>

		<div class="mt-4 space-y-5">
			<template v-for="f in CONFIG_FIELD_SET" :key="f.key || f.keys.join('-')">
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
				Enter values as JSON, for example {"company": "Acme Ltd", "benchmark_value":
				1000000, "percentage": 5}. Form fields above win on matching keys.
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
import { reactive, ref, watch } from "vue";
import { Autocomplete, Button, ErrorMessage, FormControl } from "frappe-ui";
import DocSection from "@/components/doc/DocSection.vue";
import { searchLink } from "@/api";
import {
	CONFIG_FIELD_SET,
	KNOWN_CONFIG_KEYS,
	CONFIG_FIELD_LABELS,
	NUMBER_CONFIG_KEYS,
} from "@/lib/agentConfigFields";

const props = defineProps({
	config: { type: Object, default: () => ({}) }, // parsed installation config
	saving: { type: Boolean, default: false },
});

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
		const v = c[key];
		form[key] = v == null ? "" : String(v);
	}
	const rest = {};
	for (const [key, value] of Object.entries(c)) {
		if (!KNOWN_CONFIG_KEYS.has(key)) rest[key] = value;
	}
	advanced.value = JSON.stringify(rest, null, 2);
	advancedError.value = "";
}

watch(() => props.config, seed, { immediate: true });

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
	// 2) the known fields merge OVER the JSON; an empty field drops its key
	// rather than saving "" (§14 F3 / round-2 parity, now covering every known
	// field, not only numeric ones).
	const merged = { ...base };
	for (const key of Object.keys(form)) {
		const v = String(form[key] ?? "").trim();
		if (v === "") {
			delete merged[key];
			continue;
		}
		if (NUMBER_CONFIG_KEYS.includes(key)) {
			const n = Number(v);
			if (isNaN(n)) {
				advancedError.value = `"${CONFIG_FIELD_LABELS[key]}" must be a number.`;
				return;
			}
			merged[key] = n;
		} else {
			merged[key] = v;
		}
	}
	advancedError.value = "";
	emit("save", merged);
}
</script>
