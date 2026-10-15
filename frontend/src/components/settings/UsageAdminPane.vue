<template>
	<SettingsPane
		title="User usage"
		description="Per-user token usage and limits across this workspace."
		:error="syncReason"
	>
		<template #actions>
			<Button
				variant="subtle"
				iconLeft="refresh-cw"
				:label="syncing ? 'Syncing…' : 'Sync from agent'"
				:loading="syncing"
				@click="onSync"
			/>
		</template>

		<!-- Load failure keeps its own inline recovery rather than the pane-level
		     error slot, which belongs to the sync action above. -->
		<div v-if="loadError" class="flex flex-col items-center gap-3 py-12 text-center">
			<FeatherIcon name="alert-triangle" class="size-8 text-ink-gray-4" />
			<span class="text-base text-ink-gray-6">Could not load usage.</span>
			<Button
				variant="subtle"
				label="Retry"
				iconLeft="refresh-cw"
				:loading="loading"
				@click="loadUsers"
			/>
		</div>

		<div v-else-if="loading && !users.length" class="grid place-items-center py-10">
			<JvSpinner />
		</div>

		<div v-else-if="!users.length" class="flex flex-col items-center gap-2 py-12 text-center">
			<FeatherIcon name="users" class="size-8 text-ink-gray-4" />
			<span class="text-base text-ink-gray-6">No users with settings or usage yet.</span>
		</div>

		<template v-else>
			<!-- Column ratios carried over from the stylesheet this pane used to
			     ship (1.5 / 1.8 / 1.3 / 0.9). -->
			<div
				class="grid items-center gap-3.5 pb-2 text-xs font-medium text-ink-gray-5"
				style="grid-template-columns: 1.5fr 1.8fr 1.3fr 0.9fr"
			>
				<div>User</div>
				<div>Usage</div>
				<div>Token limit</div>
				<div>Last activity</div>
			</div>

			<template v-for="u in users" :key="u.user">
				<div
					class="grid items-center gap-3.5 border-t py-3"
					style="grid-template-columns: 1.5fr 1.8fr 1.3fr 0.9fr"
				>
					<div class="flex min-w-0 items-center gap-1.5">
						<button
							v-if="(u.per_model || []).length"
							type="button"
							class="flex size-5 shrink-0 items-center justify-center rounded text-ink-gray-5 hover:bg-surface-gray-2"
							@click="toggle(u.user)"
							:aria-expanded="!!expanded[u.user]"
							:aria-label="`Per-model usage for ${u.full_name || u.user}`"
						>
							<FeatherIcon
								name="chevron-right"
								class="size-3.5 transition-transform"
								:class="{ 'rotate-90': expanded[u.user] }"
							/>
						</button>
						<span v-else class="size-5 shrink-0" />
						<div class="min-w-0">
							<div class="truncate text-sm font-medium text-ink-gray-8">
								{{ u.full_name || u.user }}
							</div>
							<div class="truncate text-xs text-ink-gray-5">{{ u.user }}</div>
						</div>
					</div>

					<div>
						<template v-if="u.monthly_token_limit > 0">
							<div class="h-1.5 overflow-hidden rounded-full bg-surface-gray-3">
								<div
									class="h-full bg-surface-gray-7"
									:style="{ width: pct(u) + '%' }"
								/>
							</div>
							<div class="mt-1 text-xs text-ink-gray-5">
								{{ fmtTokens(limitWindow(u).used) }} of
								{{ fmtTokens(u.monthly_token_limit) }} {{ limitWindow(u).label }} ·
								{{ pct(u) }}%
							</div>
						</template>
						<div v-else class="text-xs text-ink-gray-5">
							{{ fmtTokens(u.total_tokens) }} total · unlimited
						</div>
					</div>

					<div class="flex items-center gap-1">
						<span class="text-sm text-ink-gray-8">{{ limitText(u) }}</span>
						<Button
							variant="ghost"
							size="sm"
							icon="edit-2"
							:tooltip="`Edit token limit for ${u.full_name || u.user}`"
							:aria-label="`Edit token limit for ${u.full_name || u.user}`"
							@click="openEditor(u)"
						/>
					</div>

					<div class="text-xs text-ink-gray-5">
						{{ u.last_usage_at ? timeAgo(u.last_usage_at) : "-" }}
					</div>
				</div>

				<div v-if="expanded[u.user]" class="border-t bg-surface-gray-1 px-3 py-2">
					<div
						v-for="m in u.per_model || []"
						:key="m.model"
						class="grid items-center gap-3.5 py-2"
						style="grid-template-columns: 1.2fr 1.8fr 1.3fr"
					>
						<div class="truncate text-sm text-ink-gray-7">
							{{ modelDisplayLabel(m.model) }}
						</div>
						<div>
							<template v-if="m.monthly_token_limit > 0">
								<div class="h-1.5 overflow-hidden rounded-full bg-surface-gray-3">
									<div
										class="h-full bg-surface-gray-7"
										:style="{ width: modelPct(m) + '%' }"
									/>
								</div>
								<div class="mt-1 text-xs text-ink-gray-5">
									{{ fmtTokens(m.month_tokens) }} of
									{{ fmtTokens(m.monthly_token_limit) }} · {{ modelPct(m) }}%
								</div>
							</template>
							<div v-else class="text-xs text-ink-gray-5">
								{{ fmtTokens(m.month_tokens) }} · unlimited
							</div>
						</div>
						<div class="flex items-center gap-1">
							<span class="text-sm text-ink-gray-8">{{ modelLimitText(m) }}</span>
							<Button
								variant="ghost"
								size="sm"
								icon="edit-2"
								:tooltip="`Edit monthly limit for ${modelDisplayLabel(m.model)}`"
								:aria-label="`Edit monthly limit for ${modelDisplayLabel(
									m.model
								)}`"
								@click="openEditor(u, m)"
							/>
						</div>
					</div>
				</div>
			</template>
		</template>

		<TokenLimitDialog
			v-if="editor"
			v-model="editorOpen"
			:user="editor.user.user"
			:user-label="editor.user.full_name || editor.user.user"
			:model="editor.model ? editor.model.model : ''"
			:limit="Number((editor.model || editor.user).monthly_token_limit || 0)"
			:period="editor.user.limit_period || 'All time'"
			@saved="applySaved"
		/>
	</SettingsPane>
</template>

<script setup>
// Tenant-admin usage table (fleet usage spec §7). Gated at the SettingsDialog
// level by window.is_jarvis_admin — the server re-checks require_jarvis_admin()
// independently on every call, so a stale client gate can only hide the nav
// item, never bypass the real permission.
import { ref, reactive, onMounted } from "vue";
import { Button, FeatherIcon, toast } from "frappe-ui";
import { timeAgo } from "@/utils/datetime";
import { modelDisplayLabel } from "@/utils/usageModel";
import { fmtTokens, limitWindow } from "@/lib/tokens.js";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import JvSpinner from "@/components/JvSpinner.vue";
import TokenLimitDialog from "@/components/settings/TokenLimitDialog.vue";
import * as api from "@/api";
import { errMessage as errMsg, errHtml } from "@/lib/errors";

const users = ref([]);
const loading = ref(false);
const loadError = ref(false);
const expanded = reactive({});

function toggle(user) {
	expanded[user] = !expanded[user];
}

async function loadUsers() {
	loading.value = true;
	loadError.value = false;
	try {
		const res = await api.adminListUserUsage();
		if (res && res.ok === false) {
			loadError.value = true;
			return;
		}
		const rows = (res && res.data) || [];
		users.value = rows.map((u) => ({ ...u, per_model: u.per_model || [] }));
	} catch (e) {
		loadError.value = true;
	} finally {
		loading.value = false;
	}
}

// The cap reads against its window (all-time total, or this day/week/month's
// period_tokens). See jarvis.chat.policy._over_total_limit.
function pct(u) {
	if (!u || !u.monthly_token_limit) return 0;
	return Math.min(100, Math.round((limitWindow(u).used / Number(u.monthly_token_limit)) * 100));
}
function modelPct(m) {
	if (!m || !m.monthly_token_limit) return 0;
	return Math.min(
		100,
		Math.round((Number(m.month_tokens || 0) / Number(m.monthly_token_limit)) * 100)
	);
}
function limitText(u) {
	if (!u.monthly_token_limit) return "Unlimited";
	return `${fmtTokens(u.monthly_token_limit)} · ${(u.limit_period || "All time").toLowerCase()}`;
}
function modelLimitText(m) {
	return m.monthly_token_limit ? `${fmtTokens(m.monthly_token_limit)} monthly` : "Unlimited";
}

// One dialog instance, re-pointed at whichever row's pencil was clicked
// (`model` set = the per-model monthly cap, else the user's cap + window).
const editor = ref(null);
const editorOpen = ref(false);
function openEditor(u, m = null) {
	editor.value = { user: u, model: m };
	editorOpen.value = true;
}
function applySaved({ model, monthly_token_limit, limit_period }) {
	const { user: u, model: m } = editor.value;
	if (model && m) {
		m.monthly_token_limit = monthly_token_limit;
		return;
	}
	// A window switch restarts the count server-side; mirror it locally.
	if (limit_period !== (u.limit_period || "All time")) u.period_tokens = 0;
	u.monthly_token_limit = monthly_token_limit;
	u.limit_period = limit_period;
}

// "Sync from agent" — sweeps the agent gateway's sessions.list to refresh
// per-session snapshots, then reloads the table. Success reports through a
// toast rather than the old green inline note (design.md §5 anti-pattern 16);
// failure rides the pane-level error slot.
const syncing = ref(false);
const syncReason = ref("");
async function onSync() {
	syncing.value = true;
	syncReason.value = "";
	try {
		const res = await api.adminSyncUsage();
		if (res && res.ok === false) {
			syncReason.value = res.reason || "Sync failed.";
			return;
		}
		const d = (res && res.data) || {};
		toast.success(
			`Synced ${d.synced_sessions ?? 0} session${d.synced_sessions === 1 ? "" : "s"}, ${
				d.users_updated ?? 0
			} user${d.users_updated === 1 ? "" : "s"} updated`
		);
		await loadUsers();
	} catch (e) {
		syncReason.value = errMsg(e);
	} finally {
		syncing.value = false;
	}
}

onMounted(loadUsers);
</script>
