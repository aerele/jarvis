<template>
	<section>
		<div class="flex items-center gap-2">
			<div class="text-base font-medium text-ink-gray-9">Activation</div>
			<Badge
				v-if="state && state.activation_state === 'live'"
				variant="subtle"
				theme="green"
				label="Live"
			/>
			<!-- Badge has no violet theme in frappe-ui 0.1.278 - reuses the same
			     violet-pill recipe as the "Preview" pill on the Runs tab so shadow
			     reads as ONE consistent axis across the agent page. -->
			<span
				v-else-if="state"
				class="inline-flex h-5 shrink-0 select-none items-center whitespace-nowrap rounded-full bg-surface-violet-1 px-2 text-xs text-ink-violet-1"
			>
				Shadow (preview)
			</span>
		</div>

		<div v-if="loading && !state" class="mt-2 text-sm text-ink-gray-5">
			Loading activation state…
		</div>
		<div v-else-if="fetchError" class="mt-2 text-sm text-ink-red-4">
			{{ fetchError }}
			<Button variant="ghost" label="Retry" class="ml-1" @click="emit('retry')" />
		</div>
		<template v-else-if="state">
			<p class="mt-2 max-w-2xl text-sm text-ink-gray-6">
				<template v-if="state.activation_state === 'live'">
					Live<template v-if="state.promoted_at">
						since {{ timeAgo(state.promoted_at) }}</template
					><template v-if="state.promoted_by">
						· signed off by {{ state.promoted_by }}</template
					>. Its output is a compliant attestation on the owner surface.
				</template>
				<template v-else>
					Runs are visible only to the named reviewer,
					<span class="font-medium text-ink-gray-7">{{ state.reviewer || "-" }}</span
					>, and are not a compliant attestation until a reviewer promotes it.
					<span v-if="isScribe">
						This agent writes to the Org wiki directly, so it cannot run at all until
						promoted.
					</span>
				</template>
			</p>
			<p class="mt-1 text-sm text-ink-gray-5">
				Runs as
				<span class="font-medium text-ink-gray-7">{{ state.run_as_user || "-" }}</span>
			</p>

			<div class="mt-3 flex items-center gap-2">
				<Button
					v-if="state.activation_state !== 'live'"
					label="Promote to live"
					:disabled="!canAct"
					:tooltip="actionTooltip"
					@click="open('promote')"
				/>
				<Button
					v-else
					variant="subtle"
					theme="red"
					label="Demote to shadow"
					:disabled="!canAct"
					:tooltip="actionTooltip"
					@click="open('demote')"
				/>
				<span v-if="!canAct" class="text-sm text-ink-gray-5">
					Only the named reviewer or a Jarvis Admin may change this.
				</span>
			</div>
		</template>

		<Dialog
			:modelValue="dialogOpen"
			:options="{ title: dialogTitle, size: 'md' }"
			@update:modelValue="close"
		>
			<template #body-content>
				<div class="flex flex-col gap-4">
					<div
						v-if="mode === 'promote'"
						class="flex items-start gap-2 rounded-lg border border-outline-amber-2 bg-surface-amber-1 px-3 py-2 text-sm text-ink-amber-3"
					>
						<FeatherIcon name="alert-triangle" class="mt-0.5 size-4 shrink-0" />
						<div class="space-y-1">
							<div>
								This lets <span class="font-medium">{{ agentTitle }}</span> run
								unattended - on its schedule, with no one watching - as
								<span class="font-medium">{{
									(state && state.run_as_user) || "-"
								}}</span
								>.
							</div>
							<div>
								Its output becomes a live, compliant attestation on the owner
								surface immediately, replacing the reviewer-only preview.
							</div>
							<div>It also counts against this customer's live-module budget.</div>
						</div>
					</div>
					<div
						v-else
						class="flex items-start gap-2 rounded-lg border border-outline-gray-2 bg-surface-gray-1 px-3 py-2 text-sm text-ink-gray-7"
					>
						<FeatherIcon name="eye-off" class="mt-0.5 size-4 shrink-0" />
						<div>
							This stops <span class="font-medium">{{ agentTitle }}</span> from
							running live. Future runs go back to a reviewer-only preview until it
							is promoted again; existing live output is not deleted.
						</div>
					</div>

					<FormControl
						type="textarea"
						:label="
							mode === 'promote' ? 'Justification (optional)' : 'Reason (optional)'
						"
						:rows="3"
						:modelValue="note"
						@update:modelValue="(v) => (note = v)"
					/>

					<ErrorMessage :message="dialogError" />
				</div>
			</template>
			<template #actions>
				<div class="flex justify-end gap-2">
					<Button label="Cancel" :disabled="busy" @click="close" />
					<Button
						variant="solid"
						:theme="mode === 'demote' ? 'red' : undefined"
						:label="mode === 'promote' ? 'Promote to live' : 'Demote to shadow'"
						:loading="busy"
						@click="confirm"
					/>
				</div>
			</template>
		</Dialog>
	</section>
</template>

<script setup>
// ActivationPanel - jarvis#456: the missing action UI for PP-4 shadow -> live
// activation. Wraps agents_api.{promote,demote}_installation (the only
// legitimate setters of activation_state - a plain save is blocked by the
// controller's _guard_activation_transition). Presentational: the parent
// (AgentDetail) owns the fetch/refresh of `state` (via
// api/agents.getInstallationActivation, read outside get_agent's frozen §8.3
// shape - see that wrapper's comment) so the hero badge and this panel never
// drift out of sync; this component only opens the confirm dialog and calls
// the two mutating endpoints.
import { computed, ref, watch } from "vue";
import { Badge, Button, Dialog, ErrorMessage, FeatherIcon, FormControl, toast } from "frappe-ui";
import { errMessage as errMsg } from "@/lib/errors";
import { timeAgo } from "@/utils/datetime";
import * as apiAgents from "@/api/agents";

const props = defineProps({
	installationName: { type: String, required: true },
	agentTitle: { type: String, default: "This agent" },
	isScribe: { type: Boolean, default: false },
	// {activation_state, reviewer, run_as_user, promoted_by, promoted_at} | null
	state: { type: Object, default: null },
	loading: { type: Boolean, default: false },
	fetchError: { type: String, default: "" },
	// true when the viewer is the named reviewer or a Jarvis Admin - a UX guard
	// only; agents_api re-checks authority server-side regardless.
	canAct: { type: Boolean, default: false },
});

const emit = defineEmits(["promoted", "demoted", "retry"]);

const actionTooltip = computed(() =>
	props.canAct ? "" : "Only the named reviewer or a Jarvis Admin may change this"
);

// ── dialog ────────────────────────────────────────────────────────────────
const dialogOpen = ref(false);
const mode = ref("promote"); // 'promote' | 'demote'
const note = ref("");
const busy = ref(false);
const dialogError = ref("");

const dialogTitle = computed(() =>
	mode.value === "promote"
		? `Promote ${props.agentTitle} to live?`
		: `Demote ${props.agentTitle} to shadow?`
);

function open(m) {
	if (!props.canAct) return;
	mode.value = m;
	note.value = "";
	dialogError.value = "";
	dialogOpen.value = true;
}

function close() {
	if (busy.value) return; // in-flight call owns the dialog until it settles
	dialogOpen.value = false;
}

async function confirm() {
	if (busy.value) return;
	busy.value = true;
	dialogError.value = "";
	try {
		const res =
			mode.value === "promote"
				? await apiAgents.promoteInstallation(props.installationName, note.value)
				: await apiAgents.demoteInstallation(props.installationName, note.value);
		const nextState =
			mode.value === "promote"
				? { ...props.state, activation_state: "live" }
				: {
						...props.state,
						activation_state: "shadow",
						promoted_by: null,
						promoted_at: null,
				  };
		if (res && res.data) Object.assign(nextState, res.data);
		toast.success(mode.value === "promote" ? "Promoted to live" : "Demoted to shadow");
		dialogOpen.value = false;
		emit(mode.value === "promote" ? "promoted" : "demoted", nextState);
	} catch (e) {
		// Stay open and surface WHY (ceiling reached, capacity, permissions) -
		// the endpoint's message already names the exact reason - rather than a
		// generic failure the reviewer can't act on.
		dialogError.value = errMsg(e);
		toast.error(dialogError.value);
	} finally {
		busy.value = false;
	}
}

// A stale dialog must never survive an installation switch (agent nav while open).
watch(
	() => props.installationName,
	() => {
		dialogOpen.value = false;
	}
);
</script>
