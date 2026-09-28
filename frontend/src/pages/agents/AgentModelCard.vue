<template>
	<section v-if="loadError || showCard" class="mb-10 max-w-2xl min-w-0">
		<!-- Gated on loadError too, not just showCard: showCard needs info.value,
		     which is exactly what the fetch is resolving, so a showCard-only gate
		     can never surface a fetch failure (silently hides the whole card
		     forever with no way to Retry). `loading` is deliberately NOT part of
		     this gate: AgentDetail only mounts this component after its OWN probe
		     has settled (modelInfoSettled), so a fetch happening HERE means that
		     probe failed and this is the fallback retry path - render nothing
		     while it's in flight (flag-off, the common case, never even reaches
		     this component - see AgentDetail's mount guard). load() leaves
		     loadError untouched at the START of a Retry (only clears/replaces it
		     once the retry resolves), so the section - and the Retry button -
		     stay visible with a "Loading…" swap-in through the retry itself,
		     rather than vanishing mid-click. -->
		<div class="text-base font-medium text-ink-gray-9">Model</div>
		<div class="mt-1 text-sm text-ink-gray-5">Applies to everyone using this agent.</div>

		<div v-if="loading" class="mt-3 flex items-center gap-2 text-sm text-ink-gray-5">
			<JvSpinner /> Loading…
		</div>
		<div v-else-if="loadError" class="mt-3 flex items-center gap-2 text-sm text-ink-red-4">
			<span>{{ loadError }}</span>
			<Button variant="subtle" label="Retry" @click="load" />
		</div>
		<template v-else-if="showCard">
			<Banner v-if="stateText" class="mt-3" :type="stateType" :message="stateText">
				<template v-if="info.state === 'needs_model'" #action>
					<Button
						v-if="isAdmin"
						variant="subtle"
						label="Connect a provider"
						@click="$emit('connect-provider')"
					/>
				</template>
			</Banner>
			<Banner v-if="noteText" class="mt-3" type="info" :message="noteText">
				<template v-if="canApply" #action>
					<Button variant="subtle" label="Open Agents page" @click="goToAgents" />
				</template>
			</Banner>
			<Banner v-if="savedNotice" class="mt-3" type="success" :message="savedNoticeText">
				<template v-if="savedNotice.canApply" #action>
					<Button variant="subtle" label="Open Agents page" @click="goToAgents" />
				</template>
			</Banner>

			<div
				class="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-lg border px-3 py-2.5"
			>
				<div class="min-w-0">
					<div class="flex flex-wrap items-center gap-2">
						<span class="truncate text-base text-ink-gray-8">{{ currentLabel }}</span>
						<Badge
							v-if="currentTier"
							variant="subtle"
							:theme="tierThemeOf(currentTier)"
							:label="currentTier"
						/>
						<Badge
							v-if="pendingApply"
							variant="subtle"
							theme="orange"
							label="Pending apply"
						/>
						<Badge
							v-if="noneEligible"
							variant="subtle"
							theme="red"
							label="Unavailable"
						/>
					</div>
					<div class="mt-0.5 text-sm text-ink-gray-5">{{ requirementText }}</div>
					<div v-if="catalogUnknown" class="mt-1 text-sm text-ink-gray-5">
						Model list unavailable — try again shortly.
					</div>
					<div v-else-if="noneEligible" class="mt-1 text-sm text-ink-gray-5">
						None of your connected AI providers offer a model this agent needs.
						<button
							v-if="isAdmin"
							type="button"
							class="text-ink-gray-8 underline"
							@click="$emit('connect-provider')"
						>
							Connect a provider
						</button>
					</div>
				</div>
				<div class="flex shrink-0 items-center gap-2">
					<Button label="Change" :disabled="changeDisabled" @click="openPicker" />
					<Button
						v-if="isAdmin && info.state"
						variant="ghost"
						label="Use automatic choice"
						:loading="resetting"
						@click="doReset"
					/>
				</div>
			</div>
		</template>

		<!-- status changes (saved / reset / errors) are announced here for
		     screen-reader users without moving focus. -->
		<span class="sr-only" role="status" aria-live="polite">{{ liveMessage }}</span>

		<Dialog v-model="pickerOpen" :options="{ title: 'Choose a model' }">
			<template #body-content>
				<AgentModelPicker
					:eligible="eligible"
					:loading="pickerLoading"
					:current="currentRef"
					:saving="saving"
					@select="onPick"
				/>
			</template>
		</Dialog>
	</section>
</template>

<script setup>
// AgentModelCard - the Configure-tab "Model" section for the per-agent
// minimum-model floor (T6). Entirely self-contained: fetches its own
// get_agent_model/get_eligible_models, saves via set_agent_model, resets via
// reset_agent_model (admins only), and re-fetches after either so the card
// never shows a stale state/pending-apply badge.
//
// Renders NOTHING when Jarvis Settings.enforce_agent_min_model is off, or
// when the flag is on but nothing has ever governed this agent yet (no
// requirement AND no row) - see shouldShowModelCard in @/lib/agentModelTier,
// the flag-off guard this component's own tests mutation-verify.
import { ref, computed } from "vue";
import { useRouter } from "vue-router";
import { Badge, Button, Dialog, toast } from "frappe-ui";
import JvSpinner from "@/components/JvSpinner.vue";
import Banner from "@/components/Banner.vue";
import AgentModelPicker from "./AgentModelPicker.vue";
import * as apiAgents from "@/api/agents";
import { errMessage as errMsg, errHtml } from "@/lib/errors";
import {
	shouldShowModelCard,
	isPendingApply,
	isNoneEligible,
	isCatalogUnknown,
	currentModelLabel,
	requirementPhrase,
	stateBannerText,
	stateBannerType,
	noteBannerText,
	tierTheme,
} from "@/lib/agentModelTier";

const props = defineProps({
	agentSlug: { type: String, required: true },
	// getAgentsCaps().review - the same reviewer/apply capability AgentDetail
	// already probes for the Apply-catalog copy.
	canApply: { type: Boolean, default: false },
	// has_jarvis_admin_access - gates Reset and the "Connect a provider" CTA
	// (a plain user cannot reach the AI models settings pane either).
	isAdmin: { type: Boolean, default: false },
	// AgentDetail already resolves get_agent_model()/get_eligible_models() for
	// its OWN pre-install gate (Install button/confirm dialog) - handing the
	// result down here dedupes what would otherwise be a second identical
	// fetch the instant this card mounts. null while the parent's own probe
	// is still in flight (or failed): load() below is the fallback for that.
	initialInfo: { type: Object, default: null },
	initialEligible: { type: Object, default: null },
});
defineEmits(["connect-provider"]);
const router = useRouter();

const info = ref(props.initialInfo); // get_agent_model()
const eligible = ref(props.initialEligible); // get_eligible_models()
const loading = ref(false);
const loadError = ref("");
const liveMessage = ref("");
const savedNotice = ref(null); // { canApply } | null - cleared on reload/reopen

async function load() {
	// loadError is deliberately NOT cleared here (only on a resolved outcome
	// below) - see the template's gating comment: clearing it synchronously
	// would drop the outer v-if the instant Retry is clicked, vanishing the
	// section (and the Retry button) instead of swapping to "Loading…".
	loading.value = true;
	savedNotice.value = null;
	try {
		const next = (await apiAgents.getAgentModel(props.agentSlug)) || null;
		info.value = next;
		eligible.value =
			next && next.enforced
				? (await apiAgents.getEligibleModels(props.agentSlug)) || null
				: null;
		loadError.value = "";
	} catch (e) {
		info.value = null;
		eligible.value = null;
		loadError.value = errMsg(e);
	} finally {
		loading.value = false;
	}
}
// Only fetch here when the parent handed down nothing yet - a fresh mount
// with initialInfo already set (the common case: AgentDetail's own probe
// resolves long before a user clicks into Configure) skips the network
// round-trip entirely.
if (!props.initialInfo) load();

const showCard = computed(() => shouldShowModelCard(info.value));
const pendingApply = computed(() => isPendingApply(info.value));
const noneEligible = computed(() => isNoneEligible(eligible.value));
const catalogUnknown = computed(() => isCatalogUnknown(eligible.value));
const changeDisabled = computed(() => noneEligible.value || catalogUnknown.value);

const currentLabel = computed(() => currentModelLabel(info.value));
const currentTier = computed(
	() => (info.value && info.value.choice && info.value.choice.capability_tier) || ""
);
const requirementText = computed(() => requirementPhrase(info.value && info.value.required_tier));
const stateText = computed(() => stateBannerText(info.value));
const stateType = computed(() => stateBannerType(info.value));
const noteText = computed(() => noteBannerText(info.value, props.canApply));

function tierThemeOf(tier) {
	return tierTheme(tier);
}

const currentRef = computed(() => {
	const c = info.value && info.value.choice;
	return c ? { provider: c.provider, model: c.model } : null;
});

// ── the "Change" picker (Dialog + shared AgentModelPicker) ──────────────────
const pickerOpen = ref(false);
const pickerLoading = ref(false);
function openPicker() {
	if (changeDisabled.value) return;
	savedNotice.value = null;
	pickerOpen.value = true;
	refreshEligible(); // fresh eligibility for the picker, not the last-loaded snapshot
}
async function refreshEligible() {
	pickerLoading.value = true;
	try {
		eligible.value = (await apiAgents.getEligibleModels(props.agentSlug)) || null;
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		pickerLoading.value = false;
	}
}
const savedNoticeText = computed(() =>
	savedNotice.value && savedNotice.value.canApply
		? "Saved — apply catalog changes on the Agents page to use it."
		: "Saved. An admin must apply changes before it takes effect."
);
function goToAgents() {
	router.push({ name: "AgentsList" });
}
// Double-submit guard: `saving` blocks a second onPick call synchronously
// AND is passed to the picker to disable every option, so a rapid second
// click can't even fire. The request id is belt-and-braces against a
// superseded response landing after a newer one already did (latest wins).
const saving = ref(false);
let pickReqId = 0;
async function onPick({ provider, model }) {
	if (saving.value) return;
	saving.value = true;
	const id = ++pickReqId;
	try {
		const res = await apiAgents.setAgentModel(props.agentSlug, provider, model);
		if (id !== pickReqId) return; // superseded - a newer pick already landed
		info.value = res || info.value;
		pickerOpen.value = false;
		savedNotice.value = { canApply: props.canApply };
		liveMessage.value = savedNoticeText.value;
		toast.success(liveMessage.value);
	} catch (e) {
		if (id === pickReqId) toast.error(errHtml(e));
	} finally {
		if (id === pickReqId) saving.value = false;
	}
}

// ── Reset (admins only) ──────────────────────────────────────────────────────
const resetting = ref(false);
async function doReset() {
	if (resetting.value) return;
	resetting.value = true;
	try {
		info.value = (await apiAgents.resetAgentModel(props.agentSlug)) || info.value;
		liveMessage.value = "Reset to the automatic choice.";
		toast.success(liveMessage.value);
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		resetting.value = false;
	}
}

defineExpose({ load });
</script>
