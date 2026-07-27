<template>
	<SettingsPane
		title="Connection"
		description="Connection status for this workspace."
		:error="errorMessage"
	>
		<div class="rounded-md border p-4">
			<div v-if="!isSystemManager" class="text-p-sm text-ink-gray-6">
				Connection details are available to System Managers only.
			</div>
			<div v-else-if="loading" class="text-p-sm text-ink-gray-6">Checking.</div>
			<Button
				v-else-if="err"
				variant="subtle"
				label="Retry"
				iconLeft="refresh-cw"
				:loading="loading"
				@click="load"
			/>
			<template v-else>
				<KvRow label="Status">
					<Badge :label="statusLabel" :theme="statusTheme" variant="subtle" />
				</KvRow>
				<KvRow v-if="conn.default_model" label="Model" :value="conn.default_model" />
				<KvRow
					v-if="isProxy && conn.oauth_expires_at"
					label="Expires"
					:value="expiresLabel"
				/>
			</template>
		</div>
	</SettingsPane>
</template>

<script setup>
import { ref, computed, onMounted } from "vue";
import { Badge, Button } from "frappe-ui";
import { getLlmConnectionStatus } from "@/api";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import KvRow from "@/components/settings/KvRow.vue";

// Admin-tier endpoint (server enforces `require_jarvis_admin`); the rail already
// gates this pane, but guard the fetch so a non-admin never fires a doomed
// request and sees a clear note instead. PART 4 REVISED TASK 49(c): widened to
// the Jarvis Admin tenant-admin tier.
const isSystemManager = !!(window.is_system_manager || window.is_jarvis_admin);

const conn = ref({});
const loading = ref(true);
const err = ref(false);

// Deduped from AccountView + MonitorTab (the two identical Connection cards):
// oauth_expires_at is an epoch-ms value, rendered in the viewer's locale.
const expiresLabel = computed(() => {
	const ms = conn.value.oauth_expires_at;
	return ms ? new Date(Number(ms)).toLocaleString() : "—";
});

// get_llm_connection_status now short-circuits server-side for a DIRECT
// (single-model) tenant instead of surfacing the raw proxy-auth payload, so
// `proxy_active` tells the two states apart explicitly - no more guessing
// from which fields happen to be populated (that heuristic used to misread
// a direct tenant's own default_model as "connection details present" and
// render an orange "Not connected" even though chat worked fine).
const isProxy = computed(() => !!conn.value.proxy_active);
const statusLabel = computed(() => {
	if (!isProxy.value) return "Direct";
	return conn.value.auth_present ? "Connected" : "Not connected";
});
// Direct is a mode, not a warning, so it stays neutral gray; a real failure to
// connect is red (design.md §3.8 status map — matches GeneralPane's mapping
// for the same underlying fields).
const statusTheme = computed(() => {
	if (!isProxy.value) return "gray";
	return conn.value.auth_present ? "green" : "red";
});

// SettingsPane renders the one error surface for the pane (§4.1); this is the
// message it shows, derived from the `err` flag below.
const errorMessage = computed(() =>
	err.value ? "Connection status is unavailable right now." : ""
);

async function load() {
	if (!isSystemManager) {
		loading.value = false;
		return;
	}
	loading.value = true;
	err.value = false;
	try {
		conn.value = (await getLlmConnectionStatus()) || {};
	} catch (e) {
		err.value = true;
	} finally {
		loading.value = false;
	}
}

onMounted(load);
</script>
