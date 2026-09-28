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
					<Button
						variant="subtle"
						label="Change"
						:disabled="saving"
						@click="showPicker = !showPicker"
					/>
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
					:loading="installing || saving"
					:disabled="installing || saving || loading || noneEligible || catalogUnknown"
					@click="confirmInstall"
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
// UX-4 review fix: picking a model here is a LOCAL draft only, not an
// immediate set_agent_model call - the agent isn't installed yet, so Cancel
// must be able to walk away leaving nothing changed. The draft is only sent
// (set_agent_model) on Install confirm, and only when it actually differs
// from the auto-pick; install_agent's own plan_install finds that row already
// chosen and keeps it, so Install never re-sends a pick that matches the
// auto-choice.
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
// The server's own best-eligible auto-choice (load()-derived) - Install's
// "did the draft actually change" comparison below is against THIS, not
// against whatever the row happens to hold server-side.
const autoPicked = ref(null); // {provider, model}
const picked = ref(null); // {provider, model} - the LOCAL draft, starts == autoPicked
const showPicker = ref(false);
const liveMessage = ref("");
// Double-submit guard for the Install-confirm's set_agent_model call (fired
// only when the draft differs from the auto-pick) - see AgentModelCard's
// onPick for the same pattern. onPick itself below needs no guard: it is now
// a synchronous, local-only draft update, not a network call.
const saving = ref(false);
let confirmReqId = 0;

async function load() {
	loading.value = true;
	try {
		eligible.value = (await apiAgents.getEligibleModels(props.agentSlug)) || null;
		const models = (eligible.value && eligible.value.models) || [];
		if (models.length) {
			autoPicked.value = { provider: models[0].provider, model: models[0].model };
			picked.value = { ...autoPicked.value };
		}
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
		autoPicked.value = null;
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

// UX-4: local draft only - no network call. The pick is only sent (if it
// differs from the auto-choice) when Install is actually confirmed below.
function onPick({ provider, model }) {
	picked.value = { provider, model };
	showPicker.value = false;
	liveMessage.value = "Model selected.";
}

const draftChanged = computed(
	() =>
		!!(
			picked.value &&
			autoPicked.value &&
			(picked.value.provider !== autoPicked.value.provider ||
				picked.value.model !== autoPicked.value.model)
		)
);

// UX-4: Install sends the draft pick FIRST (only when it actually differs
// from the auto-choice - install_agent's plan_install already keeps a
// matching row as-is) and only emits `confirm` - which the parent turns into
// the real install_agent call - once that succeeds. A failed set_agent_model
// surfaces its error and does not install, leaving the dialog open so the
// pick can be retried or abandoned via Cancel.
async function confirmInstall() {
	if (saving.value || props.installing) return;
	if (!draftChanged.value) {
		emit("confirm");
		return;
	}
	saving.value = true;
	const id = ++confirmReqId;
	try {
		await apiAgents.setAgentModel(props.agentSlug, picked.value.provider, picked.value.model);
		if (id !== confirmReqId) return; // superseded - the dialog was reopened mid-flight
		emit("confirm");
	} catch (e) {
		if (id === confirmReqId) toast.error(errHtml(e));
	} finally {
		if (id === confirmReqId) saving.value = false;
	}
}

function onClose(v) {
	// Cancel/backdrop/Escape: the draft above is local-only and was never
	// sent, so simply closing discards it - nothing to undo server-side.
	emit("update:modelValue", !!v);
}
</script>
