<template>
	<!-- AI models pane (System Managers only — the dialog rail gates the section).
	     Ported from views/AccountView.vue's "AI models" card: a segmented
	     Chat-subscription | API-keys/failover control, a brief save note, and a
	     retryable load. SettingsDialog's CONTENT root binds paletteVars +
	     .jv-dark, so the shared jv-* classes below resolve without a local
	     palette wrapper. If that binding is ever dropped, every var(--) here and
	     in LlmPoolEditor resolves to nothing (none of them carry fallbacks). -->
	<!-- The dialog shell stopped rendering a shared header, so this pane supplies
	     its own like every other one. Only the header is migrated here: the body
	     below still uses the legacy jv-* markup because LlmPoolEditor (3,959
	     lines, drives live tenant pool provisioning) is deliberately deferred to
	     its own PR. -->
	<div class="flex h-full flex-col gap-6 px-10 py-8 text-ink-gray-8">
		<div class="flex items-start justify-between gap-4">
			<div class="flex flex-col gap-1">
				<h2 class="text-lg font-semibold text-ink-gray-8">AI models</h2>
				<p class="max-w-md text-p-sm text-ink-gray-6">
					The AI connection that powers {{ agentName }}.
				</p>
			</div>
			<span v-if="savedNote" class="shrink-0 text-p-sm text-ink-gray-6">{{
				savedNote
			}}</span>
		</div>

		<div class="jv-settings-body jv-pane-fill min-h-0 flex-1">
			<div v-if="directSubLoading" class="jv-acct-muted">Loading…</div>

			<div v-else-if="directSubErr" class="jv-acct-err">
				Couldn't load your AI connection.
				<button type="button" class="jv-mon-retry" @click="loadDirectSub">Retry</button>
			</div>

			<template v-else>
				<!-- Unified failover-list editor: a chat subscription, API keys, and
			     multi-model failover pools all live in one list + master-detail
			     config section. A legacy DIRECT (flat-field, no-proxy) subscription
			     is probed above and passed down as directStatus - LlmPoolEditor
			     synthesizes a read-oriented row for it (Reconnect embeds
			     DirectSubscriptionCard inline; Remove disconnects) without ever
			     round-tripping it through save_llm_pool. -->
				<LlmPoolEditor
					:editable="isSM"
					:directStatus="directSub"
					@saved="onSaved"
					@direct-changed="onDirectChanged"
				/>
			</template>
		</div>
	</div>
</template>

<script setup>
import { ref, onMounted } from "vue";
import { getDirectSubscriptionStatus } from "@/api";
import LlmPoolEditor from "@/components/LlmPoolEditor.vue";
import { agentName } from "@/branding";

// The rail already gates this section to the tenant-admin tier; this flag
// additionally gates the editor's edit affordances + which probes fire. PART 4
// REVISED TASK 49(c): widened to System Manager OR Jarvis Admin (the LLM-config
// endpoints are all require_jarvis_admin now).
const isSM = !!(window.is_system_manager || window.is_jarvis_admin);

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

async function loadDirectSub() {
	if (!isSM) {
		directSubLoading.value = false;
		return;
	}
	directSubLoading.value = true;
	directSubErr.value = "";
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
