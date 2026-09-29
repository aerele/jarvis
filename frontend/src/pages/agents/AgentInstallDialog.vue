<template>
	<Dialog
		:modelValue="modelValue"
		:options="{ title: dialogTitle }"
		@update:modelValue="onClose"
	>
		<!-- FE3-1: Dialog's default #body-header, with a labelled close button
		     (DialogTitle keeps reka's aria-labelledby, as in SettingsDialog.vue). -->
		<template #body-header>
			<div class="mb-6 flex items-center justify-between">
				<DialogTitle as="header">
					<h3 class="text-2xl font-semibold leading-6 text-ink-gray-9">
						{{ dialogTitle }}
					</h3>
				</DialogTitle>
				<Button variant="ghost" icon="x" label="Close" @click="onClose(false)" />
			</div>
		</template>
		<template #body-content>
			<p class="text-sm text-ink-gray-6">{{ requirementText }}</p>

			<div v-if="loading" class="mt-4 flex items-center gap-2 text-sm text-ink-gray-5">
				<JvSpinner /> Loading eligible models…
			</div>
			<div v-else-if="loadFailed" class="mt-4 text-sm text-ink-gray-5">
				Couldn't load the models this agent can use.
				<button type="button" class="text-ink-gray-8 underline" @click="load">
					Retry
				</button>
			</div>
			<div
				v-else-if="catalogUnknown && !usingExistingChoice"
				class="mt-4 text-sm text-ink-gray-5"
			>
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
						v-if="!catalogUnknown"
						variant="subtle"
						label="Change"
						@click="showPicker = !showPicker"
					/>
				</div>
				<p class="mt-2 text-sm text-ink-gray-5">
					This choice applies to everyone using this agent.
				</p>
				<p v-if="catalogUnknown" class="mt-1 text-sm text-ink-gray-5">
					Model list unavailable — installing keeps this model.
				</p>
				<div v-if="showPicker" class="mt-3">
					<AgentModelPicker :eligible="eligible" :current="pickedRef" @select="onPick" />
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
					:disabled="!canConfirm"
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
// and no separate save can race the install. The row is read fresh on open; an
// unchanged shown pick goes as `ifUnpinned`, so a pin set since never loses to it.
// The dialog closes on confirm: the page's Install button shows progress/errors.
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
	// has_jarvis_admin_access - gates the Connect-a-provider CTA, as in AgentModelCard.
	isAdmin: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue", "confirm", "connect-provider"]);

const dialogTitle = computed(() => `Install ${props.agentTitle}?`);

const loading = ref(false);
const eligible = ref(null); // get_eligible_models(); null after a failed load
const autoPicked = ref(null); // {provider, model} - what is shown before any Change
const picked = ref(null); // {provider, model} - the local draft
// EDGE2-1: seeded from an existing tenant-wide row (get_agent_model().choice).
const usingExistingChoice = ref(false);
// The row's own enriched choice - label/tier/cost when it left the eligible list.
const existingMeta = ref(null);
// A legacy/needs_model row is kept as-is by an install without a pick, so the
// shown model is sent even when unchanged (as ifUnpinned).
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
		// RES7-1: the row seeds from this read, never the page's older copy.
		const [res, info] = await Promise.all([
			apiAgents.getEligibleModels(props.agentSlug),
			apiAgents.getAgentModel(props.agentSlug),
		]);
		if (id !== loadReqId) return; // a newer open owns the display
		eligible.value = res || null;
		seed(info || null);
	} catch (e) {
		if (id !== loadReqId) return;
		eligible.value = null;
		toast.error(errHtml(e));
	} finally {
		if (id === loadReqId) {
			loading.value = false;
			// FE8-a11y: mirrors loadFailed (a thrown error OR an empty response) -
			// announce the failure to screen readers, clear it once eligible loads.
			liveMessage.value = eligible.value
				? ""
				: "Couldn't load the models this agent can use.";
		}
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
const loadFailed = computed(() => !loading.value && !eligible.value);
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
// An unreadable catalog still installs over a pinned row left as it is (no pick sent).
const canConfirm = computed(
	() =>
		!loading.value &&
		!loadFailed.value &&
		!noneEligible.value &&
		(!catalogUnknown.value || alreadySetForEveryone.value)
);
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
	if (!canConfirm.value) return;
	if (picked.value && draftChanged.value) {
		emit("confirm", { ...picked.value });
	} else if (picked.value && sendShown.value) {
		emit("confirm", { ...picked.value, ifUnpinned: true });
	} else {
		emit("confirm");
	}
}

function onClose(v) {
	emit("update:modelValue", !!v);
}
</script>
