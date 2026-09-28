<template>
	<Dialog
		:modelValue="modelValue"
		:options="{ title: `Install ${agentTitle}?` }"
		@update:modelValue="onClose"
	>
		<template #body-content>
			<p class="text-sm text-ink-gray-6">{{ requirementText }}</p>

			<div v-if="loading" class="mt-4 flex items-center gap-2 text-sm text-ink-gray-5">
				<JvSpinner /> Loading eligible models…
			</div>
			<div v-else-if="catalogUnknown" class="mt-4 text-sm text-ink-gray-5">
				Model list unavailable — try again shortly.
			</div>
			<div v-else-if="noneEligible" class="mt-4 text-sm text-ink-gray-5">
				None of your connected AI providers offer a model this agent needs.
				<button
					v-if="isAdmin"
					type="button"
					class="text-ink-gray-8 underline"
					@click="$emit('connect-provider')"
				>
					Connect a provider
				</button>
				<span v-else>Ask an admin to connect a provider that offers one.</span>
			</div>
			<template v-else>
				<div
					class="mt-4 flex items-center justify-between gap-3 rounded-lg border px-3 py-2.5"
				>
					<div class="min-w-0">
						<div class="flex flex-wrap items-center gap-2">
							<span class="truncate text-base text-ink-gray-8">{{
								pickedLabel
							}}</span>
							<Badge
								v-if="pickedTier"
								variant="subtle"
								:theme="tierThemeOf(pickedTier)"
								:label="pickedTier"
							/>
						</div>
						<div v-if="pickedCostNote" class="mt-0.5 text-sm text-ink-gray-5">
							{{ pickedCostNote }}
						</div>
					</div>
					<Button variant="subtle" label="Change" @click="showPicker = !showPicker" />
				</div>
				<div v-if="showPicker" class="mt-3">
					<AgentModelPicker
						:eligible="eligible"
						:current="pickedRef"
						:saving="saving"
						@select="onPick"
					/>
				</div>
			</template>
			<span class="sr-only" role="status" aria-live="polite">{{ liveMessage }}</span>
		</template>
		<template #actions>
			<div class="flex justify-end gap-2">
				<Button label="Cancel" @click="onClose(false)" />
				<Button
					variant="solid"
					label="Install"
					:loading="installing"
					:disabled="installing || loading || noneEligible || catalogUnknown"
					@click="$emit('confirm')"
				/>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// AgentInstallDialog - the install-time confirm step for an agent that
// declares a minimum model (T6). AgentDetail opens this INSTEAD of installing
// straight away whenever the flag is on and the listing carries a min_model;
// it shows the auto-picked model (best eligible) with a "Change" disclosure
// that reuses the SAME AgentModelPicker the Model card's dialog uses.
//
// Picking a model here calls set_agent_model immediately (jarvis#T6: this is
// deliberate, not a local draft) - the row it creates is exactly what
// install_agent's plan_install finds already chosen and keeps, so Install
// below never re-sends the pick itself, only confirms.
import { ref, computed, watch } from "vue";
import { Badge, Button, Dialog, toast } from "frappe-ui";
import JvSpinner from "@/components/JvSpinner.vue";
import AgentModelPicker from "./AgentModelPicker.vue";
import * as apiAgents from "@/api/agents";
import { errHtml } from "@/lib/errors";
import {
	isNoneEligible,
	isCatalogUnknown,
	requirementPhrase,
	tierTheme,
} from "@/lib/agentModelTier";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	agentSlug: { type: String, required: true },
	agentTitle: { type: String, default: "" },
	requiredTier: { type: String, default: "" },
	installing: { type: Boolean, default: false },
	// has_jarvis_admin_access - gates the Connect-a-provider CTA the same way
	// AgentModelCard does (a plain user cannot reach the AI models pane).
	isAdmin: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue", "confirm", "connect-provider"]);

const loading = ref(false);
const eligible = ref(null); // get_eligible_models()
const picked = ref(null); // {provider, model} the row is currently set to
const showPicker = ref(false);
const liveMessage = ref("");
// Double-submit guard - see AgentModelCard's onPick for the same pattern:
// `saving` blocks a second onPick call synchronously AND disables every
// picker option; the request id below is belt-and-braces against a
// superseded response landing after a newer one already did (latest wins).
const saving = ref(false);
let pickReqId = 0;

async function load() {
	loading.value = true;
	try {
		eligible.value = (await apiAgents.getEligibleModels(props.agentSlug)) || null;
		const models = (eligible.value && eligible.value.models) || [];
		if (models.length) picked.value = { provider: models[0].provider, model: models[0].model };
	} catch (e) {
		eligible.value = null;
		toast.error(errHtml(e));
	} finally {
		loading.value = false;
	}
}
// Re-fetch fresh + reset every time the dialog opens; never carry a stale
// pick from a previous open.
watch(
	() => props.modelValue,
	(open) => {
		if (!open) return;
		showPicker.value = false;
		picked.value = null;
		liveMessage.value = "";
		saving.value = false;
		load();
	},
	{ immediate: true }
);

const noneEligible = computed(() => isNoneEligible(eligible.value));
const catalogUnknown = computed(() => isCatalogUnknown(eligible.value));
const requirementText = computed(() => requirementPhrase(props.requiredTier));

const pickedRef = computed(() => picked.value);
const pickedMeta = computed(() => {
	const models = (eligible.value && eligible.value.models) || [];
	return (
		(picked.value &&
			models.find(
				(m) => m.provider === picked.value.provider && m.model === picked.value.model
			)) ||
		null
	);
});
const pickedLabel = computed(
	() => (pickedMeta.value && (pickedMeta.value.label || pickedMeta.value.model)) || ""
);
const pickedTier = computed(() => (pickedMeta.value && pickedMeta.value.capability_tier) || "");
const pickedCostNote = computed(() => (pickedMeta.value && pickedMeta.value.cost_note) || "");

function tierThemeOf(tier) {
	return tierTheme(tier);
}

async function onPick({ provider, model }) {
	if (saving.value) return;
	saving.value = true;
	const id = ++pickReqId;
	try {
		await apiAgents.setAgentModel(props.agentSlug, provider, model);
		if (id !== pickReqId) return; // superseded - a newer pick already landed
		picked.value = { provider, model };
		showPicker.value = false;
		liveMessage.value = "Model changed.";
	} catch (e) {
		if (id === pickReqId) toast.error(errHtml(e));
	} finally {
		if (id === pickReqId) saving.value = false;
	}
}

function onClose(v) {
	emit("update:modelValue", !!v);
}
</script>
