<template>
	<SettingsPane title="Usage" description="Message and token counts for this device.">
		<template v-if="measured">
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
					{{ fmtTokens(measured.month_tokens) }} of
					{{ fmtTokens(measured.monthly_token_limit) }} this month, {{ measuredPct }}%
				</p>
			</template>
			<p v-else class="mt-2 text-p-sm text-ink-gray-5">
				No monthly limit set on your account.
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

		<p class="flex flex-wrap items-center gap-2 text-p-sm text-ink-gray-6">
			Estimated tokens, messages and tool activity for your workspace.
			<Badge label="est." theme="gray" variant="subtle" size="sm" />
		</p>
		<div class="mt-4 grid grid-cols-3 gap-4">
			<div class="rounded-md border p-4">
				<div class="text-2xl font-medium text-ink-gray-8">{{ s ? s.msgCount : "—" }}</div>
				<div class="mt-1 text-sm text-ink-gray-6">Messages</div>
				<div class="mt-1 text-xs text-ink-gray-5">
					{{ s ? `${s.userMsgCount} you, ${s.assistantMsgCount} Jarvis` : "no chat" }}
				</div>
			</div>
			<div class="rounded-md border p-4">
				<div class="text-2xl font-medium text-ink-gray-8">
					{{ s ? s.sessionToolCalls : "—" }}
				</div>
				<div class="mt-1 text-sm text-ink-gray-6">Tool calls</div>
				<div class="mt-1 text-xs text-ink-gray-5">this session</div>
			</div>
			<div class="rounded-md border p-4">
				<div class="text-2xl font-medium text-ink-gray-8">
					{{ s ? s.avgTokensPerMsg : "—" }}
				</div>
				<div class="mt-1 text-sm text-ink-gray-6">Avg tokens per msg</div>
				<div class="mt-1 text-xs text-ink-gray-5">this chat</div>
			</div>
			<div class="rounded-md border p-4">
				<div class="text-2xl font-medium text-ink-gray-8">{{ s ? s.convCount : "—" }}</div>
				<div class="mt-1 text-sm text-ink-gray-6">Conversations</div>
				<div class="mt-1 text-xs text-ink-gray-5">
					{{ s ? `${s.starredCount} starred` : "no chat" }}
				</div>
			</div>
			<div class="rounded-md border p-4">
				<div class="text-2xl font-medium text-ink-gray-8">
					{{ usage ? fmtTokens(usage.chat_tokens) : "—" }}
				</div>
				<div class="mt-1 text-sm text-ink-gray-6">This chat</div>
				<div class="mt-1 text-xs text-ink-gray-5">tokens</div>
			</div>
			<div class="rounded-md border p-4">
				<div class="text-2xl font-medium text-ink-gray-8">
					{{ usage ? fmtTokens(usage.month_tokens) : "—" }}
				</div>
				<div class="mt-1 text-sm text-ink-gray-6">
					{{ usage ? usage.month_label : "This month" }}
				</div>
				<div class="mt-1 text-xs text-ink-gray-5">tokens</div>
			</div>
			<div class="rounded-md border p-4">
				<div class="text-2xl font-medium text-ink-gray-8">
					{{ usage ? fmtTokens(usage.total_tokens) : "—" }}
				</div>
				<div class="mt-1 text-sm text-ink-gray-6">All time</div>
				<div class="mt-1 text-xs text-ink-gray-5">tokens</div>
			</div>
			<div class="rounded-md border p-4">
				<div class="text-2xl font-medium text-ink-gray-8">{{ s ? s.toolCount : "—" }}</div>
				<div class="mt-1 text-sm text-ink-gray-6">Tools</div>
				<div class="mt-1 text-xs text-ink-gray-5">available</div>
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
	</SettingsPane>
</template>

<script setup>
import { ref, computed, onMounted, watch } from "vue";
import { Badge } from "frappe-ui";
import { useShellStore } from "@/stores/shell";
import { timeAgo } from "@/utils/datetime";
import { modelDisplayLabel } from "@/utils/usageModel";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import KvRow from "@/components/settings/KvRow.vue";
import * as api from "@/api";

const shell = useShellStore();
const s = computed(() => shell.chatContext?.sessionStats || null);

const usage = ref(null);

// Real (gateway-recorded) usage, added to get_usage()'s response. null until the
// backend ships it or the user has no recorded usage yet (self-hosted stays null).
const measured = computed(() => (usage.value && usage.value.measured) || null);
const measuredPct = computed(() => {
	const m = measured.value;
	if (!m || !m.monthly_token_limit) return 0;
	return Math.min(
		100,
		Math.round((Number(m.month_tokens || 0) / Number(m.monthly_token_limit)) * 100)
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

onMounted(loadUsage);
watch(() => shell.chatContext?.conversationId, loadUsage);
</script>
