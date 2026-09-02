<template>
	<div>
		<div v-if="!fields.length && !hasAdvanced" class="text-sm text-ink-gray-6">
			<p>
				Engagement settings this agent reads when it runs. Leave a setting empty to use the
				default.
			</p>
			<dl class="mt-3 space-y-2.5">
				<div
					v-for="k in CONFIG_KEY_REFERENCE"
					:key="k.key"
					class="flex flex-col gap-0.5 sm:flex-row sm:gap-3"
				>
					<dt class="shrink-0 sm:w-44">
						<code
							class="rounded bg-surface-gray-2 px-1.5 py-0.5 font-mono text-xs text-ink-gray-7"
						>
							{{ k.key }}
						</code>
					</dt>
					<dd class="text-ink-gray-5">{{ k.description }}</dd>
				</div>
			</dl>
		</div>

		<div v-if="fields.length" class="space-y-4">
			<template v-for="f in fields" :key="f.key">
				<Switch
					v-if="f.type === 'boolean'"
					:label="labelFor(f.key)"
					:modelValue="f.value"
					@update:modelValue="(v) => (f.value = v)"
				/>
				<FormControl
					v-else-if="f.type === 'number'"
					type="number"
					:label="labelFor(f.key)"
					:modelValue="f.value"
					@update:modelValue="(v) => (f.value = v)"
				/>
				<FormControl
					v-else
					type="text"
					:label="labelFor(f.key)"
					:modelValue="f.value"
					@update:modelValue="(v) => (f.value = v)"
				/>
			</template>
		</div>

		<!-- §14 F3: arrays/objects + unknown/new keys live in Advanced only -->
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
// ConfigForm - §14 F3: a real form generated from the installation's current
// config object. boolean → Switch, number → FormControl type=number, string →
// type=text; array/object values + unknown/new keys live in a collapsed
// "Advanced (JSON)" DocSection (mono textarea, JSON.parse-validated). Save
// merges the form values OVER the advanced JSON and emits the merged object;
// the parent persists via setAgentConfig.
import { ref, computed, watch } from "vue";
import { Button, ErrorMessage, FormControl, Switch } from "frappe-ui";
import DocSection from "@/components/doc/DocSection.vue";

// jarvis#1062 polish: the empty state used to say "add keys under Advanced
// (JSON)" with no hint of WHAT to add. This reference is the actual set the
// run path reads today - verified against agent_scope.py's _resolve
// (company/fiscal_year/from_date/to_date) and the set_config docstring in
// agents_api.py (benchmark_value/percentage/engagement_risk_level/
// rounding_step) - not a schema. A per-agent DECLARED config schema (so this
// list is generated, not hand-maintained, and a listing can add its own
// keys) is #1063; this stays a hand-maintained reference until then.
const CONFIG_KEY_REFERENCE = [
	{ key: "company", description: "Company to audit; default: your default company" },
	{ key: "fiscal_year", description: "Fiscal year to cover; default: the current one" },
	{ key: "from_date / to_date", description: "Explicit period; overrides fiscal_year" },
	{
		key: "benchmark_value / percentage",
		description:
			"Materiality: benchmark amount and percentage; auditors report checks as not evaluable when unset",
	},
	{ key: "engagement_risk_level", description: "low / medium / high" },
	{ key: "rounding_step", description: "Round-number plug detection step" },
];

const props = defineProps({
	config: { type: Object, default: () => ({}) }, // parsed installation config
	saving: { type: Boolean, default: false },
});

const emit = defineEmits(["save"]);

const fields = ref([]); // [{key, type: 'boolean'|'number'|'string', value}]
const advanced = ref("{}");
const advancedError = ref("");

function seed(cfg) {
	const scalars = [];
	const complex = {};
	for (const [key, value] of Object.entries(cfg || {})) {
		if (typeof value === "boolean") scalars.push({ key, type: "boolean", value });
		else if (typeof value === "number")
			scalars.push({ key, type: "number", value: String(value) });
		else if (typeof value === "string" || value == null)
			scalars.push({ key, type: "string", value: value == null ? "" : value });
		else complex[key] = value; // arrays + objects → Advanced
	}
	fields.value = scalars;
	advanced.value = JSON.stringify(complex, null, 2);
	advancedError.value = "";
}

watch(() => props.config, seed, { immediate: true });

const hasAdvanced = computed(() => {
	const t = advanced.value.trim();
	return t !== "" && t !== "{}";
});

function onAdvancedInput(v) {
	advanced.value = v;
	advancedError.value = "";
}

function labelFor(key) {
	return String(key)
		.split(/[_-]/)
		.map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
		.join(" ");
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
	// 2) form values merge over the JSON (§14 F3)
	const merged = { ...base };
	for (const f of fields.value) {
		if (f.type === "boolean") {
			merged[f.key] = !!f.value;
		} else if (f.type === "number") {
			const v = String(f.value ?? "").trim();
			if (v === "") {
				delete merged[f.key]; // cleared numeric → drop the key (round-2 parity)
			} else {
				const n = Number(v);
				if (isNaN(n)) {
					advancedError.value = `"${labelFor(f.key)}" must be a number.`;
					return;
				}
				merged[f.key] = n;
			}
		} else {
			merged[f.key] = f.value ?? "";
		}
	}
	advancedError.value = "";
	emit("save", merged);
}
</script>
