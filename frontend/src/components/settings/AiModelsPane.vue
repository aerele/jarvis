<template>
	<!-- AI models pane (System Managers only — the dialog rail gates the section).
	     Ported from views/AccountView.vue's "AI models" card: a segmented
	     Chat-subscription | API-keys/failover control, a brief save note, and a
	     retryable load. Now on the shared SettingsPane frame like every other
	     migrated pane, so the header and the load/error/retry states below use
	     frappe-ui + semantic tokens throughout.

	     jv-pane-fill is the one class kept: it is a settings.css layout hook,
	     not local styling, that gives LlmPoolEditor's own `.jv-pool-savebar` its
	     `margin-top:auto` sink-to-bottom behavior via the `.jv-pane-fill >
	     .jv-llm-editor` / `.jv-pane-fill .jv-pool-savebar` selectors. Dropping it
	     here would silently break LlmPoolEditor's save bar even though this PR
	     never touches that file. LlmPoolEditor (3,959 lines, drives live tenant
	     pool provisioning) is deliberately deferred to its own PR (jarvis#406);
	     it still renders inside SettingsDialog's paletteVars + .jv-dark subtree,
	     so its own jv-* CSS vars keep resolving unchanged. -->
	<SettingsPane title="AI models" :description="paneDescription" :error="errorMessage">
		<template #actions>
			<span v-if="savedNote" class="shrink-0 text-p-sm text-ink-gray-6">{{
				savedNote
			}}</span>
		</template>

		<!-- FIRST probe only. Both branches REPLACE the editor, so gating them on
		     "a probe is in flight" (which is what directSubLoading alone means)
		     unmounted LlmPoolEditor on every background re-probe and rebuilt it
		     with empty state. See loadDirectSub for why that silently broke
		     Disconnect (jarvis#574). Once a probe has succeeded the editor stays
		     mounted for the life of the pane. -->
		<div v-if="!directSubReady && directSubLoading" class="text-p-sm text-ink-gray-6">
			Loading…
		</div>

		<Button
			v-else-if="!directSubReady && directSubErr"
			variant="subtle"
			label="Retry"
			iconLeft="refresh-cw"
			:loading="directSubLoading"
			@click="loadDirectSub"
		/>

		<!-- Unified failover-list editor: a chat subscription, API keys, and
		     multi-model failover pools all live in one list + master-detail
		     config section. A legacy DIRECT (flat-field, no-proxy) subscription
		     is probed above and passed down as directStatus - LlmPoolEditor
		     synthesizes a read-oriented row for it (Reconnect embeds
		     DirectSubscriptionCard inline; Remove disconnects) without ever
		     round-tripping it through save_llm_pool. -->
		<template v-else>
			<!-- A re-probe that failed keeps its retry affordance, but BESIDE the
			     editor rather than instead of it: the last good directStatus is
			     still on screen and still correct. -->
			<Button
				v-if="directSubErr"
				class="mb-3"
				variant="subtle"
				label="Retry"
				iconLeft="refresh-cw"
				:loading="directSubLoading"
				@click="loadDirectSub"
			/>
			<div class="jv-pane-fill h-full">
				<LlmPoolEditor
					ref="poolEditor"
					:editable="isSM"
					:directStatus="directSub"
					:hostScrim="true"
					@saved="onSaved"
					@direct-changed="onDirectChanged"
				/>
			</div>
		</template>

		<!-- Pane-wide busy scrim (jarvis#559): LlmPoolEditor's own scrim only ever
		     covered its own box, so the pane's status line below it (same editor,
		     sunk to the bottom via jv-pane-fill) stayed sharp and unblurred while a
		     connect applied. hostScrim tells the editor to skip its scrim and expose
		     `busy` instead, so this one - anchored to the whole SettingsPane via its
		     `scrim` slot - can cover it. -->
		<template #scrim>
			<div
				v-if="poolEditor?.busy?.active"
				class="absolute inset-0 z-10 grid place-items-center bg-surface-white/85 backdrop-blur-sm"
			>
				<JvSpinner :size="56" :label="poolEditor.busy.label" />
			</div>
		</template>
	</SettingsPane>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from "vue";
import { Button } from "frappe-ui";
import { getDirectSubscriptionStatus } from "@/api";
import LlmPoolEditor from "@/components/LlmPoolEditor.vue";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import JvSpinner from "@/components/JvSpinner.vue";
import { isSyncDisconnected } from "@/lib/syncStatus";
import { agentName } from "@/branding";
import { useShellStore } from "@/stores/shell";

const store = useShellStore();

// Template ref onto LlmPoolEditor's exposed { save, busy } - read busy.active/
// busy.label above for the pane-wide scrim in the #scrim slot.
const poolEditor = ref(null);

// The rail already gates this section to the tenant-admin tier; this flag
// additionally gates the editor's edit affordances + which probes fire. PART 4
// REVISED TASK 49(c): widened to System Manager OR Jarvis Admin (the LLM-config
// endpoints are all require_jarvis_admin now).
const isSM = !!(window.is_system_manager || window.is_jarvis_admin);

// agentName is a boot-time constant (read once from the page payload, never
// reactive - see @/branding), so a plain string is enough here.
const paneDescription = `The AI connection that powers ${agentName}.`;

// ---- AI models: brief save acknowledgement (editor persists itself) --------
const savedNote = ref("");
let savedTimer = null;

// ---- Direct chat-subscription (flat-field OAuth path) ----------------------
// LlmPoolEditor's rows.value reads only models[]; a customer who onboarded a
// single chat subscription has an empty models[] with their creds in the flat
// llm_*/llm_oauth_* fields, so the pool editor can neither show nor
// re-authorize them from rows.value alone. Probe getDirectSubscriptionStatus
// and hand the result down as :directStatus - LlmPoolEditor synthesizes a
// row for it (embedding DirectSubscriptionCard inline) when
// is_direct_subscription is true.
const directSub = ref({ is_direct_subscription: false });
const directSubLoading = ref(true);
const directSubErr = ref("");
// True once a probe has come back cleanly, so the template can tell a FIRST load
// (nothing to render yet) from a background re-probe (the editor is live and must
// not be torn down). Never reset: a later probe failing does not un-know the last
// good answer.
const directSubReady = ref(false);
// SettingsPane's single error surface (design.md anti-pattern 16) takes a
// short user-facing string; the retry Button sits in the body, the same
// errorMessage/Retry split every migrated pane in this dialog uses.
const errorMessage = ref("");

async function loadDirectSub() {
	if (!isSM) {
		directSubLoading.value = false;
		return;
	}
	directSubLoading.value = true;
	directSubErr.value = "";
	errorMessage.value = "";
	try {
		// Race a client timeout so a hung probe can't strand the section on
		// "Loading…" forever (the pool editor renders behind it).
		const timeout = new Promise((_, rej) =>
			setTimeout(() => rej(new Error("timed out")), 12000)
		);
		directSub.value = (await Promise.race([getDirectSubscriptionStatus(), timeout])) || {
			is_direct_subscription: false,
		};
		directSubReady.value = true;
	} catch (e) {
		// Don't silently drop a real direct-subscription tenant onto the empty
		// pool editor: surface a retryable error instead of a dead end.
		//
		// Only for the FIRST probe. Blanking a directStatus the editor is already
		// rendering would delete a live subscription row off the screen because a
		// background re-probe timed out, which is the same class of lie the
		// unmount below used to tell. Keep the last good answer and let the retry
		// button say the refresh failed.
		if (!directSubReady.value) directSub.value = { is_direct_subscription: false };
		directSubErr.value =
			(e && (e.message || e._server_messages)) || "Couldn't load your AI connection.";
		errorMessage.value = "Couldn't load your AI connection.";
	} finally {
		directSubLoading.value = false;
	}
}

// LlmPoolEditor's embedded DirectSubscriptionCard emitted direct-changed
// (reauthorized/disconnected) — re-probe status so the synthesized row
// reflects the new state.
async function onDirectChanged() {
	await loadDirectSub();
}

// After a pool save: flash the note and re-probe direct status (a save can't
// migrate direct<->pool anymore - the unified editor never round-trips the
// synthesized direct row through save_llm_pool - but re-probing stays cheap
// insurance against drift).
async function onSaved(sync) {
	// A Disconnect emits through the same channel (the host has to re-probe after
	// one too), and "Saved" is the wrong word for having deleted every credential.
	if (isSyncDisconnected(sync && sync.last_sync_status)) savedNote.value = "Disconnected";
	else savedNote.value = sync && sync.pending ? "Saved, syncing…" : "Saved";
	clearTimeout(savedTimer);
	savedTimer = setTimeout(() => {
		savedNote.value = "";
	}, 4000);
	await loadDirectSub();
}

onMounted(loadDirectSub);

// True while a model change is applying, mirroring LlmPoolEditor's own
// busy.active through the poolEditor template ref above. Still exposed (some
// callers may want it off a template ref), but the load-bearing consumer is
// the watcher below: it publishes the same signal into the shell store as
// settingsApplying, which is what SettingsDialog's go() AND the store's own
// openSettings() both check before changing settingsSection (jarvis#821
// review: go() alone left every OTHER writer of settingsSection, e.g.
// GeneralPane's "AI models" buttons routed through store.openSettings, free
// to unmount this pane mid-apply).
//
// Two ways this is guaranteed to clear, never stick true:
//   1. immediate watcher: the moment busy.active flips back to false (apply
//      settles, success or failure), the next tick syncs the store.
//   2. onUnmounted: if this pane is torn down while an apply is still in
//      flight (the one path a value watcher can't observe, since Vue stops
//      watchers on unmount before a final "false" could ever fire), the store
//      flag is force-cleared directly. This is what makes the lock provably
//      un-stickable: the pane cannot vanish without also releasing the lock
//      it published.
const applying = computed(() => !!poolEditor.value?.busy?.active);
watch(applying, (v) => (store.settingsApplying = v), { immediate: true });
onUnmounted(() => {
	store.settingsApplying = false;
});

defineExpose({ applying });
</script>
