<template>
	<SettingsPane title="Usage" :description="paneDescription" :error="meteringErrorMessage">
		<template v-if="hasMeasured">
			<h3 class="text-base font-semibold text-ink-gray-9">Measured usage</h3>
			<div class="mt-2">
				<KvRow
					:label="usage.month_label || 'This month'"
					:value="fmtTokens(measured.month_tokens)"
				/>
				<KvRow label="All time" :value="fmtTokens(measured.total_tokens)" />
				<KvRow
					v-if="measured.last_usage_at"
					label="Last activity"
					:value="timeAgo(measured.last_usage_at)"
				/>
			</div>
			<template v-if="measured.monthly_token_limit > 0">
				<div class="mt-3 h-1.5 overflow-hidden rounded-full bg-surface-gray-3">
					<div class="h-full bg-surface-gray-7" :style="{ width: measuredPct + '%' }" />
				</div>
				<p class="mt-2 text-p-sm text-ink-gray-5">
					{{ fmtTokens(measured.total_tokens) }} of
					{{ fmtTokens(measured.monthly_token_limit) }} all time, {{ measuredPct }}%
				</p>
			</template>
			<p v-else class="mt-2 text-p-sm text-ink-gray-5">
				No token limit set on your account.
			</p>

			<template v-if="perModel.length">
				<h3 class="mt-6 text-base font-semibold text-ink-gray-9">By model, this month</h3>
				<div class="mt-2">
					<div v-for="m in perModel" :key="m.model" class="mt-3 first:mt-0">
						<div class="flex items-baseline justify-between gap-4">
							<span class="text-sm font-medium text-ink-gray-8">{{
								modelDisplayLabel(m.model)
							}}</span>
							<span class="text-sm text-ink-gray-6">
								{{ fmtTokens(m.month_tokens) }}
								<span class="text-ink-gray-5">
									({{ fmtTokens(m.month_input_tokens) }} in,
									{{ fmtTokens(m.month_output_tokens) }} out)
								</span>
							</span>
						</div>
						<template v-if="m.monthly_token_limit > 0">
							<div class="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-gray-3">
								<div
									class="h-full bg-surface-gray-7"
									:style="{ width: modelPct(m) + '%' }"
								/>
							</div>
							<p class="mt-1 text-p-sm text-ink-gray-5">
								{{ fmtTokens(m.month_tokens) }} of
								{{ fmtTokens(m.monthly_token_limit) }}, {{ modelPct(m) }}%
							</p>
						</template>
						<p v-else class="mt-1 text-p-sm text-ink-gray-5">Unlimited</p>
					</div>
				</div>
			</template>

			<hr class="my-8" />
		</template>

		<!-- The wording changes with the block above: naming the measured totals
		     only makes sense when they are on screen. -->
		<p class="flex flex-wrap items-center gap-2 text-p-sm text-ink-gray-6">
			<template v-if="hasMeasured">
				Workspace activity. Token figures here are estimated from the stored transcript, so
				they read lower than the measured totals above, which also count the instructions
				and history sent with every turn.
			</template>
			<template v-else>
				Estimated tokens, messages and tool activity for your workspace.
			</template>
			<Badge label="est." theme="gray" variant="subtle" size="sm" />
		</p>
		<div class="mt-4 grid grid-cols-3 gap-4">
			<div class="rounded-md border p-4">
				<div class="text-2xl font-medium text-ink-gray-8">{{ s ? s.msgCount : "-" }}</div>
				<div class="mt-1 text-sm text-ink-gray-6">Messages</div>
				<div class="mt-1 text-xs text-ink-gray-5">
					{{ s ? `${s.userMsgCount} you, ${s.assistantMsgCount} Jarvis` : "no chat" }}
				</div>
			</div>
			<div class="rounded-md border p-4">
				<div class="text-2xl font-medium text-ink-gray-8">
					{{ usage ? usage.chat_tool_calls : "-" }}
				</div>
				<div class="mt-1 text-sm text-ink-gray-6">Tool calls</div>
				<div class="mt-1 text-xs text-ink-gray-5">this chat</div>
			</div>
			<div class="rounded-md border p-4">
				<div class="text-2xl font-medium text-ink-gray-8">
					{{ s ? s.avgTokensPerMsg : "-" }}
				</div>
				<div class="mt-1 text-sm text-ink-gray-6">Avg tokens per msg</div>
				<div class="mt-1 text-xs text-ink-gray-5">this chat</div>
			</div>
			<div class="rounded-md border p-4">
				<div class="text-2xl font-medium text-ink-gray-8">{{ s ? s.convCount : "-" }}</div>
				<div class="mt-1 text-sm text-ink-gray-6">Conversations</div>
				<div class="mt-1 text-xs text-ink-gray-5">
					{{ s ? `${s.starredCount} starred` : "no chat" }}
				</div>
			</div>
			<div class="rounded-md border p-4">
				<div class="text-2xl font-medium text-ink-gray-8">
					{{
						usage && usage.context && usage.context.fresh
							? `${fmtTokens(usage.context.used)} of ${fmtTokens(
									usage.context.capacity
							  )}`
							: "-"
					}}
				</div>
				<div class="mt-1 text-sm text-ink-gray-6">This chat</div>
				<div class="mt-1 text-xs text-ink-gray-5">
					{{
						usage && usage.context && usage.context.fresh
							? "Context in use"
							: "Not measured yet"
					}}
				</div>
			</div>
			<!-- The month / all-time estimates are dropped once measured usage
			     exists: the block above already carries both labels from the
			     gateway's own counters, and showing an estimate of the stored
			     transcript beside a measurement of what was actually sent put two
			     very different numbers under one heading (#551). "This chat" stays
			     because the measured counters have no per-chat breakdown. -->
			<template v-if="!hasMeasured">
				<div class="rounded-md border p-4">
					<div class="text-2xl font-medium text-ink-gray-8">
						{{ usage ? fmtTokens(usage.month_tokens) : "-" }}
					</div>
					<div class="mt-1 text-sm text-ink-gray-6">
						{{ usage ? usage.month_label : "This month" }}
					</div>
					<div class="mt-1 text-xs text-ink-gray-5">tokens</div>
				</div>
				<div class="rounded-md border p-4">
					<div class="text-2xl font-medium text-ink-gray-8">
						{{ usage ? fmtTokens(usage.total_tokens) : "-" }}
					</div>
					<div class="mt-1 text-sm text-ink-gray-6">All time</div>
					<div class="mt-1 text-xs text-ink-gray-5">tokens</div>
				</div>
			</template>
			<!-- Not comparable with the container's own tool numbers: this is the
			     bench-side ERP tool registry (jarvis.chat.api.list_tools), which the
			     agent runtime re-exports as jarvis__<name> alongside its own built-in
			     tools and its search catalogue. Labelled so the three are not read as
			     one drifting number (#551). -->
			<div class="rounded-md border p-4">
				<div class="text-2xl font-medium text-ink-gray-8">{{ s ? s.toolCount : "-" }}</div>
				<div class="mt-1 text-sm text-ink-gray-6">ERP tools</div>
				<div class="mt-1 text-xs text-ink-gray-5">Jarvis can run here</div>
			</div>
		</div>

		<hr class="my-8" />

		<template v-if="usage && usage.budget_monthly">
			<h3 class="text-base font-semibold text-ink-gray-9">
				Tenant monthly budget (informational)
			</h3>
			<div class="mt-3 h-1.5 overflow-hidden rounded-full bg-surface-gray-3">
				<div class="h-full bg-surface-gray-7" :style="{ width: usagePct + '%' }" />
			</div>
			<p class="mt-2 text-p-sm text-ink-gray-5">
				{{ fmtTokens(usage.month_tokens) }} of {{ fmtTokens(usage.budget_monthly) }} this
				month, {{ usagePct }}%
			</p>
		</template>
		<p v-else class="text-p-sm text-ink-gray-5">
			No monthly budget set. Counts are estimated from message text.
		</p>

		<!-- Tenant-wide pool status and metered cost, folded in from the retired
		     standalone "Billing and metering" pane. Admin/System-Manager only -
		     same gate as the rail's "Account and billing" group, so an ordinary
		     member's Usage pane is unchanged from before this merge. -->
		<template v-if="canSeeMetering">
			<hr class="my-8" />

			<h3 class="text-base font-semibold text-ink-gray-9">Workspace pool</h3>
			<p class="mt-1 text-p-sm text-ink-gray-6">
				Status and metered cost for the model pool this workspace shares.
			</p>

			<div class="mt-4 flex flex-col gap-4">
				<div class="rounded-md border p-4">
					<h4 class="text-base font-semibold text-ink-gray-9">Status</h4>
					<div class="mt-2">
						<KvRow label="Mode" :value="modeLabel" />
						<!-- last_sync_status is an internal audit string ("ok (restart via
						     admin)") and this row used to print it verbatim, so a customer read
						     an already-completed restart as a chore waiting for them in Desk.
						     @/lib/syncStatus is the one place that translates it. -->
						<KvRow label="Sync" :value="syncLabel" />
						<KvRow
							v-if="meteringSync.last_sync_at"
							label="Last sync"
							:value="meteringSync.last_sync_at"
						/>
					</div>
				</div>

				<div class="rounded-md border p-4">
					<h4 class="text-base font-semibold text-ink-gray-9">Active pool</h4>
					<div class="mt-2">
						<KvRow label="Preset" :value="meteringConfig.preset || 'Custom'" />
						<KvRow
							label="Routing"
							:value="meteringConfig.routing_mode || 'failover'"
						/>
					</div>
					<div class="mt-2 flex flex-col gap-2">
						<div
							v-for="(m, i) in meteringConfig.models || []"
							:key="i"
							class="flex items-center justify-between gap-4 text-sm text-ink-gray-8"
						>
							<span>{{ m.provider }} · {{ m.model }}</span>
							<Badge
								:label="i === 0 ? 'runs every turn' : 'backup'"
								:theme="i === 0 ? 'blue' : 'gray'"
								variant="subtle"
							/>
						</div>
					</div>
				</div>

				<div class="rounded-md border p-4">
					<h4 class="flex items-center gap-2 text-base font-semibold text-ink-gray-9">
						Metered cost
						<span class="text-p-sm font-normal text-ink-gray-5">
							· {{ meteringUsage.period || "current period" }}
						</span>
					</h4>
					<Button
						v-if="meteringUsageError"
						class="mt-2"
						variant="subtle"
						label="Retry"
						iconLeft="refresh-cw"
						:loading="meteringLoading"
						@click="loadMetering"
					/>
					<p
						v-else-if="!meteringUsage.applicable"
						class="mt-2 text-p-sm text-ink-gray-5"
					>
						Metering comes from the managed proxy. This tenant's models are called
						straight from its container, so there is no proxy to meter.
					</p>
					<template v-else>
						<div class="mt-2 grid grid-cols-3 gap-4">
							<div class="rounded-md border p-4">
								<div class="text-2xl font-medium text-ink-gray-8">
									{{ meteringUsage.tokens_in }}
								</div>
								<div class="mt-1 text-sm text-ink-gray-6">Tokens in</div>
							</div>
							<div class="rounded-md border p-4">
								<div class="text-2xl font-medium text-ink-gray-8">
									{{ meteringUsage.tokens_out }}
								</div>
								<div class="mt-1 text-sm text-ink-gray-6">Tokens out</div>
							</div>
							<div class="rounded-md border p-4">
								<div class="text-2xl font-medium text-ink-gray-8">
									{{ formatUsd(meteringUsage.cost_usd) }}
								</div>
								<div class="mt-1 text-sm text-ink-gray-6">Cost</div>
							</div>
						</div>
						<div class="mt-4 flex flex-col gap-4">
							<div v-if="perModelSpec">
								<h4 class="mb-1 text-sm font-medium text-ink-gray-7">
									Tokens by model
								</h4>
								<JvChart :spec="perModelSpec" :dark="dark" />
							</div>
							<div v-if="perModelCostSpec">
								<h4 class="mb-1 text-sm font-medium text-ink-gray-7">
									Cost by model
								</h4>
								<JvChart :spec="perModelCostSpec" :dark="dark" />
							</div>
							<EChart v-if="gaugeOption" :option="gaugeOption" />
						</div>
					</template>
				</div>

				<!-- "Request log & failover history" placeholder removed — no "coming
				     soon" cards in the language (design.md §5 #18); the section returns
				     when the feature ships. -->
			</div>
		</template>
	</SettingsPane>
</template>

<script setup>
import { ref, computed, onMounted, watch } from "vue";
import { Badge, Button } from "frappe-ui";
import { useShellStore } from "@/stores/shell";
import { timeAgo } from "@/utils/datetime";
import { modelDisplayLabel } from "@/utils/usageModel";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import KvRow from "@/components/settings/KvRow.vue";
import JvChart from "@/charts/JvChart.vue";
import EChart from "@/charts/EChart.vue";
import { budgetGaugeOption, perModelBarSpec, formatUsd } from "@/charts/usageCharts.js";
import { humaniseSyncStatus } from "@/lib/syncStatus";
import { connectionModeLabel } from "@/llm/pool";
import { useJarvisTheme } from "@/theme";
import * as api from "@/api";

const shell = useShellStore();
const s = computed(() => shell.chatContext?.sessionStats || null);
const { effectiveDark: dark } = useJarvisTheme();

// Tenant-wide pool status/cost is folded into this pane below (was the
// standalone "Billing and metering" rail item). Gate matches SettingsDialog's
// "Account and billing" rail group exactly (System Manager OR Jarvis Admin),
// read off the same boot globals (jarvis/www/jarvis.py's context.boot) rather
// than inventing a second role check.
const isSM = !!window.is_system_manager;
const isAdmin = !!window.is_jarvis_admin;
const canSeeMetering = isSM || isAdmin;

const paneDescription = computed(() =>
	canSeeMetering
		? "Message and token counts for this device, plus pool status and cost for the workspace."
		: "Message and token counts for this device."
);

const usage = ref(null);

// Real (gateway-recorded) usage, added to get_usage()'s response. null until the
// backend ships it or the user has no recorded usage yet.
const measured = computed(() => (usage.value && usage.value.measured) || null);
// The backend always returns the measured block, all-zero until the gateway has
// recorded a turn. Rendering it while it is still empty put a "0 tokens" heading
// above a non-zero estimate, which is the same two-numbers-disagree problem as
// showing both once real counters exist. So the block appears only once there is
// something measured to show.
const hasMeasured = computed(() => !!(measured.value && Number(measured.value.total_tokens || 0)));
// All-time: compares against total_tokens (the cumulative, never-reset
// counter), not month_tokens. See jarvis.chat.policy._over_total_limit.
const measuredPct = computed(() => {
	const m = measured.value;
	if (!m || !m.monthly_token_limit) return 0;
	return Math.min(
		100,
		Math.round((Number(m.total_tokens || 0) / Number(m.monthly_token_limit)) * 100)
	);
});

// Per-model current-month usage + caps (fleet usage spec §7).
const perModel = computed(() => (measured.value && measured.value.per_model) || []);
function modelPct(m) {
	if (!m || !m.monthly_token_limit) return 0;
	return Math.min(
		100,
		Math.round((Number(m.month_tokens || 0) / Number(m.monthly_token_limit)) * 100)
	);
}

async function loadUsage() {
	try {
		usage.value = await api.getUsage(shell.chatContext?.conversationId);
	} catch {
		usage.value = null;
	}
}

function fmtTokens(n) {
	n = Number(n || 0);
	if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
	if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
	return String(n);
}

const usagePct = computed(() => {
	const u = usage.value;
	if (!u || !u.budget_monthly) return 0;
	return Math.min(100, Math.round((u.month_tokens / u.budget_monthly) * 100));
});

// ---- Workspace pool status/cost (admin/SM only) ----------------------------
// Moved verbatim from the retired BillingMeteringPane.vue: same fields, same
// loading/error mechanics, same charts.
const meteringConfig = ref({ models: [], proxy_active: 0 });
const meteringUsage = ref({ applicable: false, per_model: [], used_vs_limit: {} });
const meteringSync = ref({});
const meteringUsageError = ref(false);
const meteringLoading = ref(false);

// SettingsPane renders the one error surface for the pane (design.md §4.1); the
// metered-cost card keeps only the Retry button, this supplies the message it
// retries. Same mechanism the retired pane used: a plain string fed to
// SettingsPane's :error, which renders through frappe-ui's ErrorMessage (a text
// sink), never v-html (see the errHtml/errMessage split in @/lib/errors).
const meteringErrorMessage = computed(() =>
	meteringUsageError.value ? "Usage is unavailable right now." : ""
);

// `proxy_active` means "a Bifrost/cliproxy sidecar is deployed", NOT "this is a
// pool" - a pool of BYO api keys is rendered agent-direct and runs its own
// failover with no sidecar. Reading the flag alone printed "Direct" right above
// the Active-pool card listing both of that tenant's models.
//
// The wording itself lives in @/llm/pool, shared with Settings > General,
// which was naming the same three states on its own and disagreeing with this
// pane about a 2-model api-key pool (jarvis#561).
const modeLabel = computed(() =>
	connectionModeLabel(
		meteringConfig.value.proxy_active,
		(meteringConfig.value.models || []).filter((m) => m.enabled !== false).length
	)
);

// A failure reason belongs on a surface that can act on it (the AI models pane);
// here the row only needs to say which of the three states the pool is in.
const syncLabel = computed(() => humaniseSyncStatus(meteringSync.value.last_sync_status).text);

const perModelSpec = computed(() =>
	(meteringUsage.value.per_model || []).length
		? perModelBarSpec(meteringUsage.value.per_model, "tokens")
		: null
);
// Cost breakdown alongside the tokens chart - same rows, already-supported
// "cost" metric, no new data plumbing.
const perModelCostSpec = computed(() =>
	(meteringUsage.value.per_model || []).length
		? perModelBarSpec(meteringUsage.value.per_model, "cost")
		: null
);
const gaugeOption = computed(() => {
	const uv = meteringUsage.value.used_vs_limit || {};
	return budgetGaugeOption(uv.used_usd, uv.limit_usd, dark.value);
});

async function loadMeteringField(fetchFn, target) {
	try {
		target.value = (await fetchFn()) || target.value;
		return true;
	} catch (e) {
		return false;
	}
}
async function loadMetering() {
	meteringLoading.value = true;
	meteringUsageError.value = false;
	// ANY failed fetch must set meteringUsageError so the pane shows the error
	// banner and Retry instead of a confident-but-wrong resting state: a failed
	// usage fetch would render the false "single model (direct)" note, and a
	// failed config/sync fetch would render "Direct" over an empty pool list.
	const results = await Promise.all([
		loadMeteringField(api.getLlmConfig, meteringConfig),
		loadMeteringField(api.getLlmUsage, meteringUsage),
		loadMeteringField(api.getLlmSyncStatus, meteringSync),
	]);
	if (results.includes(false)) meteringUsageError.value = true;
	meteringLoading.value = false;
}

onMounted(() => {
	loadUsage();
	// Skip the round-trip entirely for a member who can never see this section -
	// the backend gates every one of these three calls server-side anyway
	// (require_jarvis_admin), so this is a UX/perf saving, not a security gate.
	if (canSeeMetering) loadMetering();
});
watch(() => shell.chatContext?.conversationId, loadUsage);
</script>
