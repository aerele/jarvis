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
			<KvRow
				v-if="isProxy && connStatus && connStatus.oauth_expires_at"
				label="Expires"
				:value="expiresLabel"
			/>
			<!-- A failed status fetch must not look like a healthy workspace. Without
			     this the catch below leaves Status on its placeholder, which reads as
			     an answer rather than as "we could not ask". Admin-only, because only
			     an admin can query the endpoint or act on the result. -->
			<div v-if="connErr" class="mt-2 flex items-center gap-2">
				<span class="text-p-sm text-ink-gray-6">
					Connection status is unavailable right now.
				</span>
				<Button
					variant="subtle"
					label="Retry"
					iconLeft="refresh-cw"
					:loading="connLoading"
					@click="loadConnStatus"
				/>
			</div>
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

		<!-- Reset workspace (jarvis.onboarding.*): self-serve container rebuild.
		     Admin-tier only; the red solid lives in the useConfirm dialog, per the
		     danger-zone rule above. While a reset runs the pane polls back to
		     Ready and hard-reloads /jarvis (drops the memoized readiness verdict). -->
		<template v-if="isSM">
			<div class="mt-6 flex items-start justify-between gap-4">
				<div class="flex flex-col gap-0.5">
					<span class="text-base font-medium text-ink-gray-8">Reset workspace</span>
					<span class="max-w-lg text-p-sm text-ink-gray-6">
						Destroys this workspace's container and attaches a fresh one, then
						reconnects automatically — use it when the workspace is stuck or
						won't connect. Chat is unavailable while it runs (usually a few
						minutes). Your subscription, chat history and AI connections are
						kept unless you tick the options.
					</span>
				</div>
				<Button
					v-if="!resetting"
					variant="subtle"
					theme="red"
					:label="resetOpen ? 'Close' : 'Reset workspace'"
					@click="resetOpen = !resetOpen"
				/>
			</div>

			<div v-if="resetting" class="mt-3">
				<Badge :label="resetStatusLabel" theme="blue" variant="subtle" />
				<p class="mt-2 text-p-sm text-ink-gray-6">{{ resetNote }}</p>
			</div>

			<div v-else-if="resetOpen" class="mt-3 max-w-lg">
				<textarea
					v-model="resetReason"
					rows="2"
					class="w-full rounded-md border bg-surface-white p-2 text-p-sm text-ink-gray-8"
					placeholder="What's wrong? (optional)"
				/>
				<label class="mt-3 flex cursor-pointer items-start gap-2.5">
					<input v-model="wipeData" type="checkbox" class="mt-0.5" />
					<span>
						<span class="block text-p-sm font-medium text-ink-gray-8">
							Also delete workspace content
						</span>
						<span class="block text-p-sm text-ink-gray-6">
							Permanently deletes chats, skills, macros, triggers, learned
							patterns, wiki pages and dashboards. Cannot be undone.
						</span>
					</span>
				</label>
				<label class="mt-2 flex cursor-pointer items-start gap-2.5">
					<input v-model="revokeLlm" type="checkbox" class="mt-0.5" />
					<span>
						<span class="block text-p-sm font-medium text-ink-gray-8">
							Also disconnect AI model connections
						</span>
						<span class="block text-p-sm text-ink-gray-6">
							Removes every connected model and key; you'll set them up again
							after the reset.
						</span>
					</span>
				</label>
				<Button
					class="mt-3"
					variant="subtle"
					theme="red"
					label="Reset workspace"
					iconLeft="refresh-cw"
					:loading="resetBusy"
					@click="doReset"
				/>
			</div>
		</template>
	</SettingsPane>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from "vue";
import { Badge, Button, toast } from "frappe-ui";
import { useShellStore } from "@/stores/shell";
import { useConfirm } from "@/composables/useConfirm";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import KvRow from "@/components/settings/KvRow.vue";
import ToggleRow from "@/components/settings/ToggleRow.vue";
import { agentName } from "@/branding";
import * as api from "@/api";

const store = useShellStore();

// Chat-scoped context (null on non-chat routes — guard everything).
const ctx = computed(() => store.chatContext);
const hasConversation = computed(() => !!(ctx.value && ctx.value.conversationId));
// Prefer the SERVER-VERIFIED default model over the conversation's label.
//
// ctx.modelLabel is scoped to the open conversation and falls back to the string
// "Auto" when there is no conversation, which is a placeholder, not a fact about
// the workspace. The deleted ConnectionPane showed conn.default_model here, and
// an admin opening Settings to diagnose a wrong-model or failover problem needs
// the configured model, not "Auto". Everything else in this block (Provider,
// Auth mode, Status) is workspace-level, so this row now matches them.
// Non-admins have no connStatus and keep the conversation label as before.
const modelLabel = computed(
	() =>
		(connStatus.value && connStatus.value.default_model) ||
		(ctx.value && ctx.value.modelLabel) ||
		"Auto"
);
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
const connErr = ref(false);
const connLoading = ref(false);

// Also ported from the removed ConnectionPane.vue: a fetch failure has to be
// visible and recoverable. Swallowing it left Status showing its placeholder,
// which an admin reads as "fine" rather than "we could not ask", and there was
// no way to retry without reloading the whole app.
async function loadConnStatus() {
	if (!isSM) return;
	connLoading.value = true;
	try {
		connStatus.value = await api.getLlmConnectionStatus();
		connErr.value = false;
	} catch (e) {
		connErr.value = true;
	} finally {
		connLoading.value = false;
	}
}
// get_llm_connection_status short-circuits server-side for a DIRECT (single-
// model) tenant and reports that via proxy_active rather than the raw proxy-auth
// payload, so proxy_active tells the two states apart explicitly instead of
// guessing from which fields happen to be populated. Without this, a direct
// tenant's own auth_present:false read as "Not connected" here too.
const isProxy = computed(() => !!(connStatus.value && connStatus.value.proxy_active));
// Ported from the removed ConnectionPane.vue, the one row this pane lacked:
// oauth_expires_at is an epoch-ms value, rendered in the viewer's locale.
const expiresLabel = computed(() => {
	const ms = connStatus.value && connStatus.value.oauth_expires_at;
	return ms ? new Date(Number(ms)).toLocaleString() : "—";
});
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
	await loadConnStatus();
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

// -- Reset workspace (danger zone) -----------------------------------------
const { confirm } = useConfirm();
const resetOpen = ref(false);
const resetReason = ref("");
const resetBusy = ref(false);
const resetting = ref(false);
const resetState = ref({});
const wipeData = ref(false);
const revokeLlm = ref(false);

// Poll every 3s while resetting, up to 15 min; the tenant-side */5 cron backstop
// converges a closed tab, so timing out here just stops the spinner.
const POLL_MS = 3000;
const POLL_BUDGET_MS = 15 * 60 * 1000;
let pollTimer = null;
let pollStarted = 0;

const resetStatusLabel = computed(() =>
	resetState.value.status === "Pending Capacity"
		? "Waiting for capacity…"
		: "Rebuilding your workspace…"
);
const resetNote = computed(() => {
	if (resetState.value.message) return resetState.value.message;
	return "This usually takes a few minutes. You can leave this page open — chat reloads when the workspace is back.";
});

async function doReset() {
	const parts = ["Chat will stop working until the workspace reconnects (usually a few minutes)."];
	if (wipeData.value) {
		parts.push(
			"All chats, skills, macros, triggers, learned patterns, wiki pages and dashboards will be permanently deleted. This cannot be undone."
		);
	} else {
		parts.push("Your subscription and chat history are kept.");
	}
	if (revokeLlm.value) {
		parts.push(
			"Your AI model connections will be disconnected — you'll set them up again after the reset."
		);
	}
	const ok = await confirm({
		title: "Reset workspace?",
		message: parts.join(" "),
		confirmLabel: "Reset workspace",
		danger: true,
	});
	if (!ok) return;
	resetBusy.value = true;
	try {
		const out =
			(await api.requestWorkspaceReset(resetReason.value, {
				wipeData: wipeData.value,
				revokeLlm: revokeLlm.value,
			})) || {};
		resetReason.value = "";
		resetOpen.value = false;
		resetState.value = out;
		resetting.value = true;
		toast.success("Resetting your workspace.");
		startPoll();
	} catch (e) {
		toast.error((e && e.messages && e.messages[0]) || "Could not reset the workspace.");
	} finally {
		resetBusy.value = false;
	}
}

function startPoll() {
	stopPoll();
	pollStarted = Date.now();
	pollTimer = setInterval(pollReset, POLL_MS);
}

function stopPoll() {
	if (pollTimer) clearInterval(pollTimer);
	pollTimer = null;
}

async function pollReset() {
	if (Date.now() - pollStarted > POLL_BUDGET_MS) {
		stopPoll();
		resetting.value = false;
		toast.error("The reset is taking longer than expected. Check back in a few minutes.");
		return;
	}
	let s;
	try {
		s = (await api.workspaceResetState()) || {};
	} catch (e) {
		return; // transient — the container is mid-rebuild; keep polling
	}
	resetState.value = s;
	if (s.ready) {
		stopPoll();
		toast.success("Workspace is back — reloading.");
		// Full reload drops the memoized readiness verdict (same ending as the
		// onboarding wizard).
		setTimeout(() => window.location.assign("/jarvis/"), 800);
	}
}

async function resumeResetIfInFlight() {
	if (!isSM) return;
	try {
		const s = (await api.workspaceResetState()) || {};
		if (s.resetting && !s.ready) {
			resetState.value = s;
			resetting.value = true;
			startPoll();
		}
	} catch (e) {
		/* no in-flight reset to resume */
	}
}

onMounted(resumeResetIfInFlight);
onBeforeUnmount(stopPoll);
</script>
