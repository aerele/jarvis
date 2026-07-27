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

		<div v-if="directSubLoading" class="text-p-sm text-ink-gray-6">Loading…</div>

		<Button
			v-else-if="directSubErr"
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
		<div v-else class="jv-pane-fill h-full">
			<LlmPoolEditor
				:editable="isSM"
				:directStatus="directSub"
				@saved="onSaved"
				@direct-changed="onDirectChanged"
			/>
		</div>
	</SettingsPane>
</template>

<script setup>
import { ref, onMounted } from "vue";
import { Button } from "frappe-ui";
import { getDirectSubscriptionStatus } from "@/api";
import LlmPoolEditor from "@/components/LlmPoolEditor.vue";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import { agentName } from "@/branding";

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
// SettingsPane's single error surface (design.md anti-pattern 16) takes a
// short user-facing string; the retry Button sits in the body, mirroring
// ConnectionPane's errorMessage/Retry split.
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
	} catch (e) {
		// Don't silently drop a real direct-subscription tenant onto the empty
		// pool editor — surface a retryable error instead of a dead end.
		directSub.value = { is_direct_subscription: false };
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
	savedNote.value = sync && sync.pending ? "Saved, syncing…" : "Saved";
	clearTimeout(savedTimer);
	savedTimer = setTimeout(() => {
		savedNote.value = "";
	}, 4000);
	await loadDirectSub();
}

onMounted(loadDirectSub);
</script>
