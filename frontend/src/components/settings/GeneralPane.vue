<template>
	<SettingsPane title="General" description="Chat behavior, notifications and token usage.">
		<h3 class="text-base font-semibold text-ink-gray-9">Connection</h3>
		<div class="mt-2">
			<KvRow label="Model" :value="modelLabel" />
			<KvRow label="Provider" :value="ui.llm_provider || '—'" />
			<KvRow label="Auth mode" :value="ui.llm_auth_mode || '—'" />
			<KvRow label="Status">
				<Badge :label="statusLabel" :theme="statusTheme" variant="subtle" />
			</KvRow>
		</div>

		<hr class="my-8" />

		<h3 class="text-base font-semibold text-ink-gray-9">Behavior</h3>
		<div class="mt-2">
			<!-- The stored flag is convAutoApply ("apply without asking"), but the
			     row reads "Confirm before changes", so the binding is inverted here
			     rather than in ToggleRow — the switch must match its own label
			     (design.md §5 anti-pattern 17). -->
			<ToggleRow
				title="Confirm before changes"
				help="Ask before creating, updating, or submitting in this chat. Deletes, cancels, amends, and emails always ask, even with this off."
				:modelValue="!convAutoApply"
				:disabled="!hasConversation"
				@update:modelValue="onToggleAutoApply"
			/>
			<p v-if="!hasConversation" class="pb-2 text-p-sm text-ink-gray-5">
				Open a conversation to change this. It is set per chat.
			</p>
			<p v-else-if="autoApplyNote" class="pb-2 text-p-sm text-ink-amber-3">
				{{ autoApplyNote }}
			</p>

			<ToggleRow
				title="Show tool activity"
				help="Show the live tool steps with input and output above each reply. The tools count and time always show below."
				:modelValue="showActivityDetail"
				@update:modelValue="setActivityDetail"
			/>
			<ToggleRow
				title="Notify when a reply is ready"
				:help="`Browser notification when ${agentName} finishes while you are in another tab.`"
				:modelValue="notifyEnabled"
				:disabled="!notifySupported"
				@update:modelValue="onToggleNotify"
			/>
			<!-- The store gates notifyEnabled on Notification.permission as well as
			     the stored preference, so this switch can read off while the server
			     row says on. Without a line here that looks like a broken toggle. -->
			<p v-if="!notifySupported" class="pb-2 text-p-sm text-ink-gray-5">
				This browser does not support notifications.
			</p>
			<p v-else-if="notifyBlocked" class="pb-2 text-p-sm text-ink-gray-5">
				Notifications are blocked for this site. Allow them in your browser settings to
				turn this on.
			</p>
		</div>

		<hr class="my-8" />

		<h3 class="flex items-center gap-2 text-base font-semibold text-ink-gray-9">
			Token usage
			<Badge label="est." theme="gray" variant="subtle" size="sm" />
		</h3>
		<div class="mt-2">
			<KvRow label="This chat" :value="usage ? fmtTokens(usage.chat_tokens) : '—'" />
			<KvRow
				:label="usage ? usage.month_label : 'This month'"
				:value="usage ? fmtTokens(usage.month_tokens) : '—'"
			/>
			<KvRow label="All time" :value="usage ? fmtTokens(usage.total_tokens) : '—'" />
		</div>
		<template v-if="usage && usage.budget_monthly">
			<div class="mt-3 h-1.5 overflow-hidden rounded-full bg-surface-gray-3">
				<div class="h-full bg-surface-gray-7" :style="{ width: usagePct + '%' }" />
			</div>
			<p class="mt-2 text-p-sm text-ink-gray-5">
				{{ fmtTokens(usage.month_tokens) }} of {{ fmtTokens(usage.budget_monthly) }} this
				month, {{ usagePct }}%
			</p>
		</template>
		<p v-else class="mt-2 text-p-sm text-ink-gray-5">
			No monthly budget set. Counts are estimated from message text.
		</p>

		<hr class="my-8" />

		<!-- Danger zone: plain heading, red SUBTLE button. The red solid lives in
		     the confirm the action opens, never resting on the pane
		     (design.md §4.1). -->
		<h3 class="text-base font-semibold text-ink-gray-9">Danger zone</h3>
		<div class="mt-2 flex items-start justify-between gap-4">
			<div class="flex flex-col gap-0.5">
				<span class="text-base font-medium text-ink-gray-8">Delete all chat history</span>
				<span class="max-w-lg text-p-sm text-ink-gray-6">
					Every conversation and message, permanently. Macros and skills stay.
				</span>
				<!-- clearAllHistory is registered by ChatView at runtime, so it is
				     absent on non-chat routes. Say why rather than showing a dead
				     disabled button. -->
				<span v-if="!canClear" class="max-w-lg text-p-sm text-ink-gray-5">
					Open a conversation to use this.
				</span>
			</div>
			<Button
				variant="subtle"
				theme="red"
				label="Delete all"
				:loading="clearing"
				:disabled="!canClear"
				@click="onClearAllHistory"
			/>
		</div>
	</SettingsPane>
</template>

<script setup>
import { ref, computed, onMounted } from "vue";
import { Badge, Button } from "frappe-ui";
import { useShellStore } from "@/stores/shell";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import KvRow from "@/components/settings/KvRow.vue";
import ToggleRow from "@/components/settings/ToggleRow.vue";
import { agentName } from "@/branding";
import * as api from "@/api";

const store = useShellStore();

// Chat-scoped context (null on non-chat routes — guard everything).
const ctx = computed(() => store.chatContext);
const hasConversation = computed(() => !!(ctx.value && ctx.value.conversationId));
const modelLabel = computed(() => (ctx.value && ctx.value.modelLabel) || "Auto");
const ui = computed(() => (ctx.value && ctx.value.ui) || {});
const convAutoApply = computed(() => !!(ctx.value && ctx.value.convAutoApply));
const autoApplyNote = computed(() => (ctx.value && ctx.value.autoApplyNote) || "");

// Real connection status. getLlmConnectionStatus is admin-tier on the server
// (require_jarvis_admin) and General is an all-user pane, so only tenant-admin
// users get the live verdict; regular users (who cannot query it and cannot fix
// it anyway) keep the benign "Connected" the surface implied before, never a
// 403 rendered as an error.
const isSM = !!(window.is_system_manager || window.is_jarvis_admin);
const connStatus = ref(null);
// get_llm_connection_status short-circuits server-side for a DIRECT (single-
// model) tenant and reports that via proxy_active rather than the raw proxy-auth
// payload — the same fix as ConnectionPane.vue. Without this, a direct tenant's
// own auth_present:false read as "Not connected" here too.
const isProxy = computed(() => !!(connStatus.value && connStatus.value.proxy_active));
const connected = computed(() =>
	isSM ? !!(connStatus.value && isProxy.value && connStatus.value.auth_present) : true
);
const statusLabel = computed(() => {
	if (!isSM) return "Connected";
	if (!connStatus.value) return "—";
	if (!isProxy.value) return "Direct";
	return connected.value ? "Connected" : "Not connected";
});
// design.md §3.8 status map: connected is green, a plain direct tenant is
// neutral, an actual failure is red.
const statusTheme = computed(() => {
	if (!isSM) return "green";
	if (!connStatus.value) return "gray";
	if (!isProxy.value) return "gray";
	return connected.value ? "green" : "red";
});

// Estimated token usage — the dialog fetches its own data on open.
const usage = ref(null);
const usagePct = computed(() => {
	const u = usage.value;
	if (!u || !u.budget_monthly) return 0;
	return Math.min(100, Math.round((u.month_tokens / u.budget_monthly) * 100));
});
function fmtTokens(n) {
	n = Number(n || 0);
	if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
	if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
	return String(n);
}

onMounted(async () => {
	try {
		usage.value = await api.getUsage(ctx.value && ctx.value.conversationId);
	} catch (e) {
		/* usage is best-effort — leave the placeholder */
	}
	if (isSM) {
		try {
			connStatus.value = await api.getLlmConnectionStatus();
		} catch (e) {
			/* status stays on the placeholder */
		}
	}
	// Roam notify/activity-detail prefs from the server row, falling back to the
	// localStorage cache the store already booted from on any failure (endpoint
	// not deployed yet, network error) — never blocks or errors the pane.
	try {
		const res = await api.getMySettings();
		if (res && res.ok !== false && res.data) store.syncSettingsFromServer(res.data);
	} catch (e) {
		/* prefs stay on the localStorage cache */
	}
});

// Confirm-before-changes → per-conversation action registered by ChatView.
function onToggleAutoApply() {
	const fn = store.settingsActions.toggleAutoApply;
	if (typeof fn === "function") fn();
}

// Device-local prefs live in the shell store (single source of truth) so that
// toggling here also updates ChatView's live gating same-tab. Read + delegate.
const showActivityDetail = computed(() => store.activityDetail);
function setActivityDetail(v) {
	store.setActivityDetail(v);
}
const notifyEnabled = computed(() => store.notifyEnabled);
// Notification.permission is not reactive, so snapshot it on mount and again
// after each toggle attempt (the store may prompt, and the answer changes it).
const notifySupported = typeof Notification !== "undefined";
const notifyPermission = ref(notifySupported ? Notification.permission : "unsupported");
const notifyBlocked = computed(() => notifyPermission.value === "denied");
async function onToggleNotify() {
	await store.toggleNotify();
	if (notifySupported) notifyPermission.value = Notification.permission;
}

// Delete all history → danger-zone action registered by ChatView.
const clearing = ref(false);
const canClear = computed(() => typeof store.settingsActions.clearAllHistory === "function");
async function onClearAllHistory() {
	const fn = store.settingsActions.clearAllHistory;
	if (typeof fn !== "function") return;
	clearing.value = true;
	try {
		await Promise.resolve(fn());
	} finally {
		clearing.value = false;
	}
}
</script>
