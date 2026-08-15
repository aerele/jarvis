<template>
	<div>
		<div v-if="!fields.length && !hasAdvanced" class="text-sm text-ink-gray-5">
			No configuration set yet - add keys under Advanced (JSON).
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
				Array/object values and new keys are edited here; form fields above win on matching
				keys.
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
