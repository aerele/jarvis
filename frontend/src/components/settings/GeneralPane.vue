<template>
	<SettingsPane title="General" description="Chat behavior, notifications and token usage.">
		<h3 class="text-base font-semibold text-ink-gray-9">Connection</h3>
		<div class="mt-2">
			<!-- Mode names the TOPOLOGY, Status reports its HEALTH. They used to be
			     the same row, which is how a 2-model pool came to be labelled
			     "Direct" here while Billing and metering called the identical state
			     "Pool (direct failover)". Both panes now read the one
			     connectionModeLabel(). -->
			<KvRow v-if="modeLabel" label="Mode" :value="modeLabel" />
			<!-- A failover pool has no single Model/Provider/Auth mode. Those three
			     rows were filled from the legacy models[0] mirror, so a 4-model pool
			     described member one and presented it as the whole connection, under
			     a Model row naming the synthetic Bifrost endpoint nobody chose. AI
			     models owns the per-model story (every model in failover order, with
			     its own status); this is a summary that points at it. The triple
			     stays for a single-credential tenant, where it is accurate. -->
			<KvRow v-if="isPool" label="Models" :value="poolSummary" />
			<template v-else>
				<KvRow label="Model" :value="modelLabel" />
				<KvRow label="Provider" :value="ui.llm_provider || '—'" />
				<KvRow label="Auth mode" :value="ui.llm_auth_mode || '—'" />
			</template>
			<KvRow label="Status">
				<Badge :label="statusLabel" :theme="statusTheme" variant="subtle" />
			</KvRow>
			<KvRow
				v-if="isProxy && connStatus && connStatus.oauth_expires_at"
				label="Expires"
				:value="expiresLabel"
			/>
			<div v-if="isPool" class="mt-2">
				<Button
					variant="subtle"
					label="Manage models"
					iconLeft="cpu"
					@click="store.openSettings('aimodels')"
				/>
			</div>
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
		     Ready and hard-reloads /jarvis (drops the memoized readiness verdict) -
		     UNLESS the poll reports `disconnected` (L4): that bench just lost its
		     admin credentials, so reloading would strand it behind the onboarding
		     gate poster instead of showing it how to get back. See benchDisconnected. -->
		<template v-if="isSM">
			<div class="mt-6 flex items-start justify-between gap-4">
				<div class="flex flex-col gap-0.5">
					<span class="text-base font-medium text-ink-gray-8">Reset workspace</span>
					<span class="max-w-lg text-p-sm text-ink-gray-6">
						Destroys this workspace's container and attaches a fresh one, then
						reconnects automatically — use it when the workspace is stuck or won't
						connect. Chat is unavailable while it runs (usually a few minutes). Pick
						how deep to go below; each level keeps everything the one above it kept.
					</span>
				</div>
				<Button
					v-if="!resetting && !resetOpen && !benchDisconnected"
					variant="subtle"
					theme="red"
					label="Reset workspace"
					@click="openReset"
				/>
			</div>

			<div v-if="benchDisconnected" class="mt-3 max-w-lg">
				<div
					class="rounded-lg border border-outline-red-2 bg-surface-red-1 px-3 py-2 text-sm text-ink-red-4"
				>
					<p class="font-medium">This bench is disconnected.</p>
					<p class="mt-1">{{ disconnectRecoveryText }}</p>
				</div>
				<Button
					class="mt-3"
					variant="subtle"
					theme="red"
					label="Reconnect this bench"
					iconLeft="external-link"
					@click="goReconnect"
				/>
			</div>

			<div v-else-if="resetting" class="mt-3">
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
				<!-- One escalating choice, not four independent checkboxes: the depths
				     are cumulative (L2 implies L1's rebuild, L4 implies L1-L3), so
				     separate toggles could combine into a depth request_workspace_reset
				     has no name for. A radio ladder makes "L3 without L2" unrepresentable
				     instead of merely undocumented. -->
				<fieldset class="mt-3">
					<legend class="text-p-sm font-medium text-ink-gray-8">How deep?</legend>
					<label
						v-for="d in RESET_DEPTHS"
						:key="d.value"
						class="mt-2 flex cursor-pointer items-start gap-2.5"
					>
						<input
							v-model="resetDepth"
							type="radio"
							name="reset-depth"
							:value="d.value"
							class="mt-0.5"
						/>
						<span>
							<span class="block text-p-sm font-medium text-ink-gray-8">{{ d.title }}</span>
							<span class="block text-p-sm text-ink-gray-6">{{ d.description }}</span>
						</span>
					</label>
				</fieldset>
				<!-- L4 is a lockout path (it ends with no admin credentials on this
				     bench): shown the moment it is SELECTED, not just inside the confirm
				     dialog at submit time - the plan requires the cost and the way back
				     to be visible before the choice can be made, not just before it's
				     final. -->
				<div
					v-if="resetDepth === DEPTH_DISCONNECT"
					class="mt-3 rounded-lg border border-outline-red-2 bg-surface-red-1 px-3 py-2 text-sm text-ink-red-4"
				>
					This is the deepest reset: once the rebuild finishes, this bench also
					disconnects from your account. Reconnect with the one-time code emailed
					to this workspace's registered address (plus its company name, if that
					address covers more than one). Refused up front — before anything is
					touched — if your subscription would not be eligible to reconnect
					afterwards.
				</div>
				<div ref="resetFormEl" class="mt-4 flex items-center gap-2">
					<Button
						variant="subtle"
						theme="red"
						:label="resetDepth === DEPTH_DISCONNECT ? 'Reset and disconnect' : 'Reset workspace'"
						iconLeft="refresh-cw"
						:loading="resetBusy"
						@click="doReset"
					/>
					<Button
						variant="ghost"
						label="Cancel"
						:disabled="resetBusy"
						@click="closeReset"
					/>
				</div>
			</div>

			<!-- Disconnect this bench: the SEPARATE terminal action (T3,
			     disconnect_bench). Deliberately not a fifth reset depth in the
			     ladder above - no rebuild, no poll, and it is worded for what it
			     is (leaving), not folded into "reset". Always a lockout path, so
			     the cost + recovery text is in the static description, not gated
			     behind opening a form first. -->
			<div v-if="!benchDisconnected" class="mt-8 flex items-start justify-between gap-4">
				<div class="flex flex-col gap-0.5">
					<span class="text-base font-medium text-ink-gray-8">Disconnect this bench</span>
					<span class="max-w-lg text-p-sm text-ink-gray-6">
						Leaves your account rather than resetting anything — no rebuild, no
						polling. Clears this bench's connection to your subscription; chat stops
						working immediately. The only way back is a one-time code emailed to
						this workspace's registered address (plus its company name, if that
						address covers more than one).
					</span>
					<span v-if="resetting" class="max-w-lg text-p-sm text-ink-gray-5">
						A reset is in progress — wait for it to finish before disconnecting.
					</span>
				</div>
				<Button
					variant="subtle"
					theme="red"
					label="Disconnect"
					iconLeft="log-out"
					:loading="disconnectBusy"
					:disabled="resetting"
					@click="doDisconnectBench"
				/>
			</div>
		</template>
	</SettingsPane>
</template>

<script setup>
import { ref, computed, nextTick, onMounted, onBeforeUnmount } from "vue";
import { Badge, Button, toast } from "frappe-ui";
import { useShellStore } from "@/stores/shell";
import { useConfirm } from "@/composables/useConfirm";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import KvRow from "@/components/settings/KvRow.vue";
import ToggleRow from "@/components/settings/ToggleRow.vue";
import { connectionModeLabel } from "@/llm/pool";
import { humaniseSyncStatus } from "@/lib/syncStatus";
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
// A disconnected workspace has no model, and the "Auto" fallback below would
// claim one - it is a placeholder for "the conversation did not name a model",
// which is a different thing from "there is no model".
const modelLabel = computed(() =>
	disconnected.value
		? "—"
		: (connStatus.value && connStatus.value.default_model) ||
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
// proxy_active means "a Bifrost + CLIProxyAPI sidecar pair is deployed", which
// only a chat subscription needs. It is NOT "this is a pool": a pool of BYO api
// keys renders openclaw-direct and fails over with no sidecar at all. Kept
// separate from isPool below for exactly that reason: only a proxied tenant has
// an OAuth profile expiry to show.
const isProxy = computed(() => !!(connStatus.value && connStatus.value.proxy_active));
// pool_mode is the server's compute_pool_mode: this workspace syncs as a whole
// models[] spec, so there is no one credential for the Model/Provider/Auth-mode
// triple to describe.
const isPool = computed(() => !!(connStatus.value && connStatus.value.pool_mode));
// Ported from the removed ConnectionPane.vue, the one row this pane lacked:
// oauth_expires_at is an epoch-ms value, rendered in the viewer's locale.
const expiresLabel = computed(() => {
	const ms = connStatus.value && connStatus.value.oauth_expires_at;
	return ms ? new Date(Number(ms)).toLocaleString() : "—";
});
// Shared with Billing and metering so the two panes cannot name the same state
// differently again: that pane called a 2-model api-key pool "Pool (direct
// failover)" while this one called it "Direct". Blank without a verdict (a
// non-admin never fetches one), which hides the row rather than guessing.
const modeLabel = computed(() =>
	connStatus.value
		? connectionModeLabel(connStatus.value.proxy_active, connStatus.value.model_count)
		: ""
);
// The one line that replaces the triple for a pool: how many models, how they
// are routed, and whether the container has the current set. humaniseSyncStatus
// is the same translator Billing and metering's Sync row uses, so the two cannot
// describe one last_sync_status differently. Its text is a standalone label
// there and a clause here, hence the case fold.
const poolSummary = computed(() => {
	const c = connStatus.value || {};
	const n = Number(c.model_count || 0);
	const parts = [`${n} ${n === 1 ? "model" : "models"}`];
	if (c.routing_mode) parts.push(c.routing_mode);
	const sync = humaniseSyncStatus(c.sync_status);
	if (sync.kind !== "unknown") parts.push(sync.text.toLowerCase());
	return parts.join(", ");
});
// No credential configured at all: the workspace was disconnected (or never
// connected). Checked FIRST, for the same reason the server computes it before
// its own DIRECT short-circuit: a disconnected tenant is proxy_active:false, so
// without this it would read as a healthy single-model tenant.
const disconnected = computed(() => !!(connStatus.value && connStatus.value.disconnected));
// The server's verdict, and the ONLY input to the badge. It used to be computed
// here from admin's auth_profile_present, which is a claim about a cliproxy auth
// profile and never described a pool: a 4-model pool that was serving turns off
// two connected subscriptions reported auth_profile_present:false and rendered
// red "Not connected" over working chat (jarvis#561). get_llm_connection_status
// now derives health from the same evidence is_ready_for_chat gates chat on, so
// a workspace chat let you into cannot show a failure here.
const health = computed(() => (connStatus.value && connStatus.value.health) || "");
const statusLabel = computed(() => {
	if (!isSM) return "Connected";
	if (!connStatus.value) return "—";
	if (disconnected.value) return "Disconnected";
	if (health.value === "down") return "Not connected";
	if (health.value === "applying") return "Applying changes";
	if (health.value === "attention") return "Needs attention";
	return "Connected";
});
// design.md §3.6 status map: Success is green, Attention required and Broken are
// red, Processing is blue. Disconnected is orange (warning), not red: nothing is
// broken, the customer chose this and can undo it in AI models.
const statusTheme = computed(() => {
	if (!isSM) return "green";
	if (!connStatus.value) return "gray";
	if (disconnected.value) return "orange";
	if (health.value === "down" || health.value === "attention") return "red";
	if (health.value === "applying") return "blue";
	return "green";
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

// The four depths (jarvis/onboarding.py request_workspace_reset), each adding
// to the one above — see RESET_DEPTHS below for the ladder itself.
const DEPTH_REBUILD = 1;
const DEPTH_WIPE_DATA = 2;
const DEPTH_REVOKE_LLM = 3;
const DEPTH_DISCONNECT = 4;
const resetDepth = ref(DEPTH_REBUILD);
const wipeData = computed(() => resetDepth.value >= DEPTH_WIPE_DATA);
const revokeLlm = computed(() => resetDepth.value >= DEPTH_REVOKE_LLM);
const disconnectAfter = computed(() => resetDepth.value >= DEPTH_DISCONNECT);

const RESET_DEPTHS = [
	{
		value: DEPTH_REBUILD,
		title: "Rebuild the container",
		description:
			"Destroys and rebuilds the container, then reconnects automatically. Your subscription, chat history and AI connections are kept.",
	},
	{
		value: DEPTH_WIPE_DATA,
		title: "+ Delete workspace content",
		description:
			"Also permanently deletes chats, skills, macros, triggers, learned patterns, wiki pages and dashboards. Cannot be undone.",
	},
	{
		value: DEPTH_REVOKE_LLM,
		title: "+ Disconnect AI model connections",
		description:
			"Also removes every connected model and key; you'll set them up again after the reset.",
	},
	{
		value: DEPTH_DISCONNECT,
		title: "+ Disconnect this bench",
		description:
			"Also clears this bench's connection to your account once the rebuild finishes. Refused up front if your subscription would not be eligible to reconnect afterwards.",
	},
];

// Terminal state shared by L4 (via the poll, see pollReset) and the separate
// Disconnect action below: this bench has no admin credentials left.
// benchNeedsCompany is always definitive (true/false), never null — L4's own
// request/poll fetches it from bench_connection_state() on the poll response,
// and disconnect_bench() reports it directly (see disconnectRecoveryText).
const benchDisconnected = ref(false);
const benchNeedsCompany = ref(false);
const disconnectRecoveryText = computed(() => {
	const base =
		"Reconnect with the one-time code emailed to this workspace's registered address.";
	if (benchNeedsCompany.value === true) {
		return `${base} That address is linked to more than one company, so you'll also need to give the company name.`;
	}
	return base;
});
// The wizard owns the reconnect flow (code entry, company disambiguation);
// land on it rather than duplicating that screen here — same convention as
// ChatView.vue's goReconnect for the site_replaced billing notice.
function goReconnect() {
	window.location.assign("/jarvis/onboarding");
}

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

const resetFormEl = ref(null);

function openReset() {
	resetOpen.value = true;
	nextTick(() => resetFormEl.value?.scrollIntoView({ behavior: "smooth", block: "nearest" }));
}

function closeReset() {
	resetOpen.value = false;
	resetReason.value = "";
	resetDepth.value = DEPTH_REBUILD;
}

async function doReset() {
	const parts = [
		"Chat will stop working until the workspace reconnects (usually a few minutes).",
	];
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
	if (disconnectAfter.value) {
		parts.push(
			"Once the rebuild finishes, this bench also disconnects from your account — chat stays unavailable until you reconnect with a one-time code emailed to this workspace's registered address (plus its company name, if that address covers more than one). Refused up front, before anything changes, if your subscription would not be eligible to reconnect."
		);
	}
	const ok = await confirm({
		title: disconnectAfter.value ? "Reset and disconnect this bench?" : "Reset workspace?",
		message: parts.join(" "),
		confirmLabel: disconnectAfter.value ? "Reset and disconnect" : "Reset workspace",
		danger: true,
	});
	if (!ok) return;
	resetBusy.value = true;
	try {
		const out =
			(await api.requestWorkspaceReset(resetReason.value, {
				wipeData: wipeData.value,
				revokeLlm: revokeLlm.value,
				disconnectAfter: disconnectAfter.value,
			})) || {};
		closeReset();
		resetState.value = out;
		resetting.value = true;
		toast.success("Resetting your workspace.");
		startPoll();
	} catch (e) {
		// L4's refusal (ineligible to reconnect) throws BEFORE anything is
		// rebuilt or cleared (onboarding.py request_workspace_reset) — the
		// server's message explains why, so it is shown verbatim rather than
		// replaced with a generic one.
		toast.error((e && e.messages && e.messages[0]) || "Could not reset the workspace.");
		// ...but a refusal is not the only way this call fails. Admin rebuilds
		// SYNCHRONOUSLY inside the request, so a slow reset can outlive the web
		// worker's own ceiling (gunicorn http_timeout, ~120s by default) and this
		// call dies while the reset it started proceeds. The bench keeps its claim
		// on purpose for exactly that case (onboarding.py: a timeout is not
		// evidence the rebuild did not start), and reconcile_pending_workspace_reset
		// converges it — but the customer would sit on a bare error toast watching
		// nothing happen, and reload into a workspace mid-rebuild with no
		// explanation. Start polling instead: workspace_reset_state is read-only
		// and reports `resetting: false` almost immediately if there is in fact no
		// reset, which just stops the spinner.
		if (isServerUnreachable(e)) {
			resetting.value = true;
			startPoll();
		}
	} finally {
		resetBusy.value = false;
	}
}

// Disconnect this bench (danger zone, separate from the ladder above) -------
const disconnectBusy = ref(false);

async function doDisconnectBench() {
	if (resetting.value) return; // also guarded by the button's :disabled — defensive against a stray click racing the poll
	const ok = await confirm({
		title: "Disconnect this bench?",
		message:
			"This clears this bench's connection to your account — chat stops working immediately and nothing rebuilds automatically. The only way back is a one-time code emailed to this workspace's registered address (plus its company name, if that address covers more than one). Refused up front, before anything changes, if your subscription would not be eligible to reconnect.",
		confirmLabel: "Disconnect",
		danger: true,
	});
	if (!ok) return;
	disconnectBusy.value = true;
	try {
		const res = (await api.disconnectBench()) || {};
		benchNeedsCompany.value = res.needs_company === true;
		benchDisconnected.value = true;
		toast.success(
			res.already_disconnected
				? "This bench was already disconnected."
				: "This bench is now disconnected."
		);
	} catch (e) {
		toast.error((e && e.messages && e.messages[0]) || "Could not disconnect this bench.");
	} finally {
		disconnectBusy.value = false;
	}
}

// Did the call fail because the server never answered, rather than because it
// answered "no"? Only the former means the reset may be running regardless.
//
// A server-side REFUSAL arrives with Frappe's `messages` (from _server_messages,
// which frappe.throw populates) or an explicit 4xx: request_workspace_reset
// refuses that way for an ineligible L4, a different-depth reset and a
// mid-disconnect reset, and in every one of those nothing was started. A dead
// worker, a proxy timeout or a dropped connection has no message and no 4xx.
// Defaults to FALSE for anything ambiguous: spuriously polling would leave a
// spinner over a workspace that is not resetting.
function isServerUnreachable(e) {
	const status = (e && (e.status || (e.response && e.response.status))) || 0;
	// STATUS first, because `messages` is not the reliable discriminator the first
	// draft assumed: frappe-ui's call.js synthesises `messages: ['Internal Server
	// Error']` for ANY parseable JSON error body with no _server_messages, so a
	// JSON-bodied 502/504 carries a message and would have been misread as a
	// refusal. A 5xx is the server failing regardless of what it says.
	if (status >= 500) return true;
	// A 4xx is a decision: request_workspace_reset refuses this way for an
	// ineligible L4, a different-depth reset, a non-cumulative depth and a
	// mid-disconnect reset, and in every one of those nothing was started.
	if (status >= 400) return false;
	// No status at all: a dead worker, a dropped connection, a proxy that never
	// answered. Server-side messages here mean the request WAS served, so they
	// still rule it out.
	if (e && e.messages && e.messages.length) return false;
	return status === 0;
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
	// L4's admin-connection clear only runs once this same poll has observed
	// the rebuilt container Ready and persisted the fresh connection
	// (onboarding.py _workspace_reset_poll), so `disconnected` and `ready` land
	// on the SAME response. Check `disconnected` FIRST and return: this bench
	// has no admin credentials on every later poll, and the `ready` reload
	// below would bounce it behind the onboarding gate poster instead of
	// showing it the way back — an unreachable-admin blip must never look like
	// this deliberate, permanent state, and vice versa.
	if (s.disconnected) {
		stopPoll();
		resetting.value = false;
		// Fetch the definitive needs_company from local settings so the recovery
		// text is never hedged (bench_connection_state returns {disconnected,
		// needs_company} read from local settings with no admin call).
		try {
			const state = (await api.benchConnectionState()) || {};
			benchNeedsCompany.value = state.needs_company === true;
		} catch (e) {
			// If the endpoint fails, default to false (no company name needed)
			// rather than hedging. A disconnected bench with no local settings
			// answer is rare, and "no company needed" is the less disruptive guess.
			benchNeedsCompany.value = false;
		}
		benchDisconnected.value = true;
		toast.success("Workspace reset — this bench is now disconnected.");
		return;
	}
	if (s.ready) {
		stopPoll();
		// The customer asked for L4, confirmed an irreversibility warning, and got
		// L3: the workspace WAS rebuilt, but this bench is still connected. Say so
		// rather than toasting plain success, and do NOT reload — the reload would
		// drop the only message explaining it, and there is nothing to reload for
		// because the connection is unchanged. Server side: _workspace_reset_poll's
		// `disconnect_blocked`, set when the eligibility re-check refused (the
		// subscription went ineligible mid-rebuild) or the container teardown could
		// not complete.
		if (s.disconnect_blocked) {
			resetting.value = false;
			toast.error(
				`Your workspace was reset, but this bench could not be disconnected: ${s.disconnect_blocked}`
			);
			return;
		}
		toast.success("Workspace is back — reloading.");
		// Full reload drops the memoized readiness verdict (same ending as the
		// onboarding wizard).
		setTimeout(() => window.location.assign("/jarvis/"), 800);
	}
}

async function resumeResetIfInFlight() {
	if (!isSM) return;
	// The credential-free check comes FIRST, and the ORDER is the fix — but not for
	// the reason this comment used to give (round-5 MINOR 10). It claimed
	// workspaceResetState() throws for a disconnected bench. It does not:
	// require_jarvis_admin is a role check independent of connection state, and the
	// get_connection failure is caught inside _workspace_reset_poll, which returns a
	// normal 200 with ready:false.
	//
	// The real reason: that 200 says "not resetting, not ready", which is
	// indistinguishable from "nothing to resume" — so a disconnected bench fell
	// through to the generic onboarding poster with no way back shown.
	// bench_connection_state answers the question that actually distinguishes them,
	// from local settings, with no admin call.
	try {
		const local = (await api.benchConnectionState()) || {};
		if (local.disconnected) {
			benchNeedsCompany.value = local.needs_company === true;
			benchDisconnected.value = true;
			return;
		}
	} catch (e) {
		// Local endpoint unavailable: fall through to the reset poll below rather
		// than assuming either state.
	}
	try {
		const s = (await api.workspaceResetState()) || {};
		if (s.disconnected) {
			// _workspace_reset_poll is not read-only — persisting the fresh
			// connection and clearing admin creds are side effects of calling it,
			// not just facts it reports. So THIS mount's own resume call can be
			// the one that finishes an L4 left mid-poll by a closed tab. Without
			// this check that leaves the bench disconnected server-side with the
			// pane showing no trace of it — same distinct-terminal-state
			// requirement as pollReset below, just reached from a fresh mount
			// instead of an in-progress poll.
			resetState.value = s;
			// Fetch the definitive needs_company from local settings so the
			// recovery text on reload is never hedged.
			try {
				const state = (await api.benchConnectionState()) || {};
				benchNeedsCompany.value = state.needs_company === true;
			} catch (e) {
				// If the endpoint fails, default to false (no company name needed)
				// rather than hedging.
				benchNeedsCompany.value = false;
			}
			benchDisconnected.value = true;
			return;
		}
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
