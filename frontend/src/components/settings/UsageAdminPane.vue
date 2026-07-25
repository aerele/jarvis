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

		<p v-else-if="loading && !users.length" class="text-p-base text-ink-gray-6">Loading…</p>

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
				<div>This month</div>
				<div>Monthly limit</div>
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
								{{ fmtTokens(u.month_tokens) }} of
								{{ fmtTokens(u.monthly_token_limit) }} · {{ pct(u) }}%
							</div>
						</template>
						<div v-else class="text-xs text-ink-gray-5">
							{{ fmtTokens(u.month_tokens) }} this month · unlimited
						</div>
						<div class="mt-0.5 text-xs text-ink-gray-4">
							{{ fmtTokens(u.total_tokens) }} total
						</div>
					</div>

					<div class="flex items-center gap-2">
						<FormControl
							type="number"
							size="sm"
							class="min-w-0 flex-1"
							v-model.number="u._limitDraft"
							:disabled="u._saving"
							placeholder="0 = unlimited"
						/>
						<Button
							variant="subtle"
							size="sm"
							label="Save"
							:loading="u._saving"
							:disabled="
								u._saving ||
								Number(u._limitDraft || 0) === Number(u.monthly_token_limit || 0)
							"
							@click="saveLimit(u)"
						/>
					</div>

					<div class="text-xs text-ink-gray-5">
						{{ u.last_usage_at ? timeAgo(u.last_usage_at) : "—" }}
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
						<div class="flex items-center gap-2">
							<FormControl
								type="number"
								size="sm"
								class="min-w-0 flex-1"
								v-model.number="m._limitDraft"
								:disabled="m._saving"
								placeholder="0 = unlimited"
							/>
							<Button
								variant="subtle"
								size="sm"
								label="Save"
								:loading="m._saving"
								:disabled="
									m._saving ||
									Number(m._limitDraft || 0) ===
										Number(m.monthly_token_limit || 0)
								"
								@click="saveModelLimit(u, m)"
							/>
						</div>
					</div>
				</div>
			</template>
		</template>
	</SettingsPane>
</template>

<script setup>
// Tenant-admin usage table (fleet usage spec §7). Gated at the SettingsDialog
// level by window.is_jarvis_admin — the server re-checks require_jarvis_admin()
// independently on every call, so a stale client gate can only hide the nav
// item, never bypass the real permission.
import { ref, reactive, onMounted } from "vue";
import { Button, FeatherIcon, FormControl, toast } from "frappe-ui";
import { timeAgo } from "@/utils/datetime";
import { modelDisplayLabel } from "@/utils/usageModel";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import * as api from "@/api";

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

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
		users.value = rows.map((u) => ({
			...u,
			_limitDraft: u.monthly_token_limit || 0,
			_saving: false,
			per_model: (u.per_model || []).map((m) => ({
				...m,
				_limitDraft: m.monthly_token_limit || 0,
				_saving: false,
			})),
		}));
	} catch (e) {
		loadError.value = true;
	} finally {
		loading.value = false;
	}
}

function fmtTokens(n) {
	n = Number(n || 0);
	if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
	if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
	return String(n);
}
function pct(u) {
	if (!u || !u.monthly_token_limit) return 0;
	return Math.min(
		100,
		Math.round((Number(u.month_tokens || 0) / Number(u.monthly_token_limit)) * 100)
	);
}
function modelPct(m) {
	if (!m || !m.monthly_token_limit) return 0;
	return Math.min(
		100,
		Math.round((Number(m.month_tokens || 0) / Number(m.monthly_token_limit)) * 100)
	);
}

async function saveLimit(u) {
	const val = Math.max(0, Math.round(Number(u._limitDraft) || 0));
	u._saving = true;
	try {
		const res = await api.adminSetUserLimit(u.user, val);
		if (res && res.ok === false) {
			toast.error(res.reason || "Could not update the limit.");
			return;
		}
		const d = (res && res.data) || {};
		u.monthly_token_limit = d.monthly_token_limit != null ? d.monthly_token_limit : val;
		u._limitDraft = u.monthly_token_limit;
		toast.success("Limit updated");
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		u._saving = false;
	}
}

async function saveModelLimit(u, m) {
	const val = Math.max(0, Math.round(Number(m._limitDraft) || 0));
	m._saving = true;
	try {
		const res = await api.adminSetUserModelLimit(u.user, m.model, val);
		if (res && res.ok === false) {
			toast.error(res.reason || "Could not update the model limit.");
			return;
		}
		const d = (res && res.data) || {};
		m.monthly_token_limit = d.monthly_token_limit != null ? d.monthly_token_limit : val;
		m._limitDraft = m.monthly_token_limit;
		toast.success(`Limit updated for ${modelDisplayLabel(m.model)}`);
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		m._saving = false;
	}
}

// "Sync from agent" — sweeps the openclaw gateway's sessions.list to refresh
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
