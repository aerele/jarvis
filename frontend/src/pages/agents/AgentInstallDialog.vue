<template>
	<Dialog
		:modelValue="modelValue"
		:options="{ title: dialogTitle }"
		@update:modelValue="onClose"
	>
		<!-- FE3-1 review fix: Dialog's own default #body-header renders an X that
		     closes unconditionally - while saving it was inert (onClose ignores
		     it) but, unlike Cancel, gave no disabled/aria-disabled signal. This
		     reproduces that default (title markup + DialogTitle for the
		     aria-labelledby reka wires up - see SettingsDialog.vue's #body
		     override for the same requirement) with the close button gated the
		     same way Cancel is. -->
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
					:disabled="saving"
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
				<Button label="Cancel" :disabled="saving" @click="onClose(false)" />
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
	// EDGE2-1: get_agent_model()'s {state, choice} - when a tenant-wide row
	// already exists (another installer pinned a model), plan_install never
	// overwrites it on install, so THIS is what will actually run. Seeds the
	// dialog's auto-pick instead of recomputing our own from eligible models.
	modelInfo: { type: Object, default: null },
	installing: { type: Boolean, default: false },
	// has_jarvis_admin_access - gates the Connect-a-provider CTA the same way
	// AgentModelCard does (a plain user cannot reach the AI models pane).
	isAdmin: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue", "confirm", "connect-provider"]);

// Shared by the Dialog `options.title` (drives the mock/portal a11y name) and
// the #body-header override below, so the two can never drift apart.
const dialogTitle = computed(() => `Install ${props.agentTitle}?`);

// FE3-2 review fix: ~15s ceiling on the Install-confirm's setAgentModel call -
// same Promise.race idiom as OnboardingView's GSTIN fetch timeout - so a
// stalled request can no longer lock the dialog (Cancel disabled, close inert)
// indefinitely.
const SET_MODEL_TIMEOUT_MS = 15000;

const loading = ref(false);
const eligible = ref(null); // get_eligible_models()
// The server's own best-eligible auto-choice (load()-derived) - Install's
// "did the draft actually change" comparison below is against THIS, not
// against whatever the row happens to hold server-side.
const autoPicked = ref(null); // {provider, model}
const picked = ref(null); // {provider, model} - the LOCAL draft, starts == autoPicked
// EDGE2-1: true when autoPicked/picked were seeded from an existing tenant-wide
// row (props.modelInfo.choice) rather than our own get_eligible_models best-pick.
const usingExistingChoice = ref(false);
// The existing row's own enriched choice (label/tier/cost) - a fallback source
// for pickedMeta when that model has since fallen out of the eligible list.
const existingMeta = ref(null);
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
		const existing = props.modelInfo && props.modelInfo.choice;
		if (existing && existing.provider && existing.model) {
			// A tenant-wide row already exists (auto/chosen/changed all carry a
			// choice) - install_agent's plan_install leaves it alone, so it is
			// what will actually run. Show it instead of our own auto-pick.
			usingExistingChoice.value = true;
			autoPicked.value = { provider: existing.provider, model: existing.model };
			picked.value = { ...autoPicked.value };
			existingMeta.value = existing;
		} else if (models.length) {
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
		// FE2-1: a reopen (even one forced past onClose's saving guard below)
		// must supersede any confirmInstall still awaiting a PRIOR open's
		// setAgentModel - its late resolution must never emit confirm or a
		// toast into a dialog the user has since left.
		confirmReqId++;
		showPicker.value = false;
		autoPicked.value = null;
		picked.value = null;
		usingExistingChoice.value = false;
		existingMeta.value = null;
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
	const p = picked.value;
	if (!p) return null;
	const models = (eligible.value && eligible.value.models) || [];
	const fromEligible = models.find((m) => m.provider === p.provider && m.model === p.model);
	if (fromEligible) return fromEligible;
	// EDGE2-1: an existing tenant-wide pick that has since fallen out of the
	// eligible list still needs a label/tier/cost to show - fall back to the
	// row's own enriched choice rather than rendering nothing.
	const em = existingMeta.value;
	if (em && em.provider === p.provider && em.model === p.model) return em;
	return null;
});
// EDGE2-1: shown next to the box while the draft still matches the seeded
// existing row - hides the instant Change picks something else.
const alreadySetForEveryone = computed(() => usingExistingChoice.value && !draftChanged.value);
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
	const request = apiAgents.setAgentModel(
		props.agentSlug,
		picked.value.provider,
		picked.value.model
	);
	// FE3-2 review fix: `request` is the ONLY continuation of the real call, so
	// it keeps respecting `id` even after the race below has given up on it -
	// same guard as every other stale-attempt check in this file.
	request.then(
		() => {
			if (id !== confirmReqId) return; // superseded - reopen/close, or the timeout below
			saving.value = false;
			emit("confirm");
		},
		(e) => {
			if (id !== confirmReqId) return;
			saving.value = false;
			toast.error(errHtml(e));
		}
	);
	let timedOut = false;
	let timer;
	try {
		await Promise.race([
			request,
			new Promise((_, reject) => {
				timer = setTimeout(() => {
					timedOut = true;
					reject(new Error("Saving the model took too long. Try again."));
				}, SET_MODEL_TIMEOUT_MS);
			}),
		]);
	} catch (e) {
		// A rejection of `request` itself is already handled by its own .then
		// above - only act here for the timeout, so a genuine failure never
		// double-toasts.
		if (timedOut && id === confirmReqId) {
			saving.value = false;
			toast.error(errHtml(e));
			confirmReqId++; // request may still resolve/reject later - keep it silent then
		}
	} finally {
		clearTimeout(timer);
	}
}

function onClose(v) {
	// FE2-1 review fix: Cancel/backdrop/Escape while confirmInstall's
	// setAgentModel is in flight must not close the dialog - the save is
	// sub-second, so just ignore the attempt and let it settle rather than
	// risk a stale success emitting confirm (installing) after the user left.
	// The Cancel button is also disabled while saving (see template).
	if (saving.value) return;
	// Belt-and-braces alongside the reopen-branch bump above: supersedes any
	// confirmInstall this close raced past, so its resolution never emits
	// confirm or a toast into a dialog that is now closed.
	confirmReqId++;
	// Cancel/backdrop/Escape outside a save: the draft above is local-only
	// and was never sent, so simply closing discards it - nothing to undo
	// server-side.
	emit("update:modelValue", !!v);
}
</script>
