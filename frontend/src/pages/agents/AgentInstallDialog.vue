<template>
	<Dialog
		:modelValue="modelValue"
		:options="{ title: dialogTitle }"
		@update:modelValue="onClose"
	>
		<!-- FE3-1: Dialog's default #body-header X closes unconditionally; this
		     reproduces it (DialogTitle keeps reka's aria-labelledby, as in
		     SettingsDialog.vue) with the close button gated like Cancel. -->
		<template #body-header>
			<div class="mb-6 flex items-center justify-between">
				<DialogTitle as="header">
					<h3 class="text-2xl font-semibold leading-6 text-ink-gray-9">
						{{ dialogTitle }}
					</h3>
				</DialogTitle>
				<Button
					variant="ghost"
					icon="x"
					label="Close"
					:disabled="installing"
					@click="onClose(false)"
				/>
			</div>
		</template>
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
						<div v-if="alreadySetForEveryone" class="mt-0.5 text-sm text-ink-gray-5">
							Already set for everyone using this agent.
						</div>
					</div>
					<Button
						variant="subtle"
						label="Change"
						:disabled="installing"
						@click="showPicker = !showPicker"
					/>
				</div>
				<div v-if="showPicker" class="mt-3">
					<AgentModelPicker
						:eligible="eligible"
						:current="pickedRef"
						:saving="installing"
						@select="onPick"
					/>
				</div>
			</template>
			<span class="sr-only" role="status" aria-live="polite">{{ liveMessage }}</span>
		</template>
		<template #actions>
			<div class="flex justify-end gap-2">
				<Button label="Cancel" :disabled="installing" @click="onClose(false)" />
				<Button
					variant="solid"
					label="Install"
					:loading="installing"
					:disabled="installing || loading || noneEligible || catalogUnknown"
					@click="confirmInstall"
				/>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// AgentInstallDialog - the install-time confirm step for an agent that
// declares a minimum model (T6). Shows the model the agent will run on (a
// pinned tenant-wide choice, else the best eligible) with a "Change" picker.
//
// The pick is a LOCAL draft: nothing is written here. Install emits `confirm`
// with the draft when the server would not run it otherwise (changed, or a
// legacy/needs_model row), and the parent sends it WITH install_agent, which
// validates and saves it in the same request - so Cancel leaves nothing behind
// and no separate save can race the install.
import { ref, computed, watch } from "vue";
import { Badge, Button, Dialog, toast } from "frappe-ui";
import { DialogTitle } from "reka-ui";
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
	// EDGE2-1: get_agent_model()'s {state, choice} - a pinned row's choice is
	// what runs unless changed, so it seeds the display.
	modelInfo: { type: Object, default: null },
	// The parent's install_agent is in flight: Install shows loading and
	// Cancel/X/Escape are held so a close can't race it.
	installing: { type: Boolean, default: false },
	// has_jarvis_admin_access - gates the Connect-a-provider CTA, as in AgentModelCard.
	isAdmin: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue", "confirm", "connect-provider"]);

const dialogTitle = computed(() => `Install ${props.agentTitle}?`);

const loading = ref(false);
const eligible = ref(null); // get_eligible_models()
const autoPicked = ref(null); // {provider, model} - what is shown before any Change
const picked = ref(null); // {provider, model} - the local draft
// EDGE2-1: seeded from an existing tenant-wide row (props.modelInfo.choice).
const usingExistingChoice = ref(false);
// The row's own enriched choice - label/tier/cost when it left the eligible list.
const existingMeta = ref(null);
// A legacy/needs_model row is kept as-is by an install without a pick, so the
// shown model must be sent even when unchanged.
const sendShown = ref(false);
const showPicker = ref(false);
const liveMessage = ref("");
let loadReqId = 0;

// Mirrors agent_models._PINNED: rows that run their own choice.
const PINNED = ["auto", "chosen", "changed"];
// provider is "" on a subscription lane - never treat it as missing.
const refOf = (m) => ({ provider: m.provider || "", model: m.model });
const sameRef = (a, b) => (a.provider || "") === (b.provider || "") && a.model === b.model;

function seed(modelInfo) {
	const state = modelInfo && modelInfo.state;
	const existing = modelInfo && modelInfo.choice;
	usingExistingChoice.value = !!(PINNED.includes(state) && existing && existing.model);
	existingMeta.value = usingExistingChoice.value ? existing : null;
	if (usingExistingChoice.value) {
		autoPicked.value = refOf(existing);
	} else {
		const models = (eligible.value && eligible.value.models) || [];
		autoPicked.value = models.length ? refOf(models[0]) : null;
	}
	picked.value = autoPicked.value && { ...autoPicked.value };
	sendShown.value = !!state && !usingExistingChoice.value;
}

async function load() {
	const id = ++loadReqId;
	loading.value = true;
	try {
		const res = (await apiAgents.getEligibleModels(props.agentSlug)) || null;
		if (id !== loadReqId) return; // a newer open owns the display
		eligible.value = res;
		seed(props.modelInfo);
	} catch (e) {
		if (id !== loadReqId) return;
		eligible.value = null;
		toast.error(errHtml(e));
	} finally {
		if (id === loadReqId) loading.value = false;
	}
}

// Re-fetch and reset on every open; a draft never carries over.
watch(
	() => props.modelValue,
	(open) => {
		if (!open) return;
		showPicker.value = false;
		autoPicked.value = null;
		picked.value = null;
		usingExistingChoice.value = false;
		existingMeta.value = null;
		sendShown.value = false;
		liveMessage.value = "";
		load();
	},
	{ immediate: true }
);

const noneEligible = computed(() => isNoneEligible(eligible.value));
const catalogUnknown = computed(() => isCatalogUnknown(eligible.value));
const requirementText = computed(() => requirementPhrase(props.requiredTier));

const pickedRef = computed(() => picked.value);
const pickedMeta = computed(() => {
	const p = picked.value;
	if (!p) return null;
	const models = (eligible.value && eligible.value.models) || [];
	const fromEligible = models.find((m) => sameRef(m, p));
	if (fromEligible) return fromEligible;
	const em = existingMeta.value;
	if (em && sameRef(em, p)) return em;
	return null;
});
const draftChanged = computed(
	() => !!(picked.value && autoPicked.value && !sameRef(picked.value, autoPicked.value))
);
const alreadySetForEveryone = computed(() => usingExistingChoice.value && !draftChanged.value);
const pickedLabel = computed(
	() => (pickedMeta.value && (pickedMeta.value.label || pickedMeta.value.model)) || ""
);
const pickedTier = computed(() => (pickedMeta.value && pickedMeta.value.capability_tier) || "");
const pickedCostNote = computed(() => (pickedMeta.value && pickedMeta.value.cost_note) || "");

function tierThemeOf(tier) {
	return tierTheme(tier);
}

function onPick(m) {
	picked.value = refOf(m);
	showPicker.value = false;
	liveMessage.value = "Model selected.";
}

// An unchanged draft over a pinned row (or no row) sends no pick: the server
// keeps the row, or auto-picks the same best eligible model.
function confirmInstall() {
	if (props.installing) return;
	if (picked.value && (draftChanged.value || sendShown.value)) {
		emit("confirm", { ...picked.value });
	} else {
		emit("confirm");
	}
}

function onClose(v) {
	if (props.installing) return;
	emit("update:modelValue", !!v);
}
</script>
