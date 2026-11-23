<template>
	<div class="flex min-h-0 flex-col overflow-hidden">
		<div v-if="agents.length" class="flex flex-wrap items-center justify-end gap-3 px-5 pt-4">
			<span v-if="atCap" class="text-p-sm text-ink-gray-5">
				You can have up to {{ maxAgents }} agents. Delete one to add another.
			</span>
			<Button
				variant="solid"
				label="New agent"
				iconLeft="plus"
				:disabled="atCap"
				@click="openNew"
			/>
		</div>

		<div class="min-h-0 flex-1 overflow-y-auto pb-8 pt-2">
			<div
				v-if="loading && !agents.length"
				class="py-10 text-center text-sm text-ink-gray-5"
			>
				Loading your agents…
			</div>
			<div
				v-else-if="error && !agents.length"
				class="flex flex-col items-center gap-3 py-16"
			>
				<span class="text-sm text-ink-red-4">{{ error }}</span>
				<Button label="Try again" :loading="loading" @click="emit('reload')" />
			</div>
			<div
				v-else-if="!agents.length"
				class="flex flex-col items-center gap-1 py-16 text-center"
			>
				<FeatherIcon name="cpu" class="size-7.5 text-ink-gray-5" />
				<span class="mt-2 text-lg font-medium text-ink-gray-8">No agents yet</span>
				<span class="text-p-base text-ink-gray-6">
					Build an agent with its own instructions and tools, then chat with it or let it
					run on a schedule.
				</span>
				<Button class="mt-3" variant="solid" label="New agent" @click="openNew" />
			</div>

			<ListView
				v-else
				class="!w-full"
				:columns="COLUMNS"
				:rows="agents"
				row-key="name"
				:options="{
					selectable: false,
					rowHeight: 56,
					resizeColumn: false,
					showTooltip: true,
				}"
			>
				<template #default>
					<ListHeader class="mx-3 sm:mx-5">
						<ListHeaderItem v-for="c in COLUMNS" :key="c.key" :item="c" />
					</ListHeader>
					<ListRows class="mx-3 sm:mx-5">
						<PlainRows />
					</ListRows>
				</template>
				<template #cell="{ column, row }">
					<div class="text-base">
						<template v-if="column.key === 'enabled'">
							<div class="flex w-full items-center">
								<Switch
									:modelValue="row.enabled"
									:disabled="busy === row.name + ':enabled'"
									@update:modelValue="(v) => toggleEnabled(row, v)"
								/>
							</div>
						</template>

						<template v-else-if="column.key === 'agent_name'">
							<div class="min-w-0">
								<div
									class="truncate text-base font-medium text-ink-gray-9"
									:title="row.agent_name"
								>
									{{ row.agent_name }}
								</div>
								<!-- The stored sentence is text, not a tooltip: it has to be
								     readable without hovering, and it wraps rather than
								     truncates, so a support code at its end stays visible. -->
								<div
									v-if="syncMessage(row)"
									class="whitespace-normal break-words text-sm text-ink-red-4"
								>
									{{ syncMessage(row) }}
								</div>
								<div
									v-else-if="row.description"
									class="truncate text-sm text-ink-gray-5"
									:title="row.description"
								>
									{{ row.description }}
								</div>
							</div>
						</template>

						<template v-else-if="column.key === 'state'">
							<Badge
								variant="subtle"
								:theme="stateBadge(row).theme"
								:label="stateBadge(row).label"
							/>
						</template>

						<template v-else-if="column.key === 'schedule'">
							<div
								class="truncate text-base"
								:class="
									row.schedule.enabled ? 'text-ink-gray-7' : 'text-ink-gray-4'
								"
								:title="scheduleSummary(row)"
							>
								{{ scheduleSummary(row) }}
							</div>
						</template>

						<template v-else-if="column.key === 'last_run'">
							<template v-if="lastRun(row)">
								<Tooltip :text="lastRunHint(row)">
									<div
										class="truncate text-base"
										:class="
											lastRun(row).status === 'Failed'
												? 'text-ink-red-4'
												: 'text-ink-gray-7'
										"
									>
										{{ lastRun(row).status }}
										<template v-if="lastRun(row).at">
											{{ timeAgo(lastRun(row).at) }}
										</template>
									</div>
								</Tooltip>
							</template>
							<span v-else class="text-base text-ink-gray-4">-</span>
						</template>

						<template v-else-if="column.key === '_actions'">
							<div class="flex w-full items-center justify-end gap-1">
								<Button
									label="Chat"
									:loading="busy === row.name + ':chat'"
									:disabled="!row.ready || !!busy"
									:tooltip="row.ready ? '' : notReadyReason(row)"
									@click="chat(row)"
								/>
								<Button
									variant="ghost"
									icon="play"
									label="Run now"
									:loading="busy === row.name + ':run'"
									:disabled="!row.ready || !row.schedule.prompt || !!busy"
									:tooltip="runReason(row)"
									@click="runNow(row)"
								/>
								<Button
									variant="ghost"
									icon="edit-2"
									label="Edit"
									tooltip="Edit"
									@click="openEdit(row)"
								/>
								<Dropdown :options="menuOptions(row)">
									<Button
										icon="more-horizontal"
										variant="ghost"
										label="More actions"
									/>
								</Dropdown>
							</div>
						</template>
					</div>
				</template>
			</ListView>
		</div>

		<UserAgentDialog
			v-model="dialogOpen"
			:agent="editingAgent"
			:browser="browser"
			@update:modelValue="(open) => !open && (editingName = '')"
			@saved="onSaved"
		/>
	</div>
</template>

<script setup>
// My agents tab of the Agents page: the signed-in user's own agents (user agents v1).
// One row per agent with its state, schedule and last run; Chat and Run now open the
// conversation the server returns, Edit opens the dialog, and turn on/off and Delete
// are here too. The list and its polling live in useUserAgents (the host owns them,
// the tab only draws and acts); this file owns the row actions.
import { computed, ref } from "vue";
import { useRouter } from "vue-router";
import {
	Badge,
	Button,
	Dropdown,
	FeatherIcon,
	ListHeader,
	ListHeaderItem,
	ListRows,
	ListView,
	Switch,
	Tooltip,
	confirmDialog,
	toast,
} from "frappe-ui";
import PlainRows from "@/components/list/PlainRows.vue";
import UserAgentDialog from "./UserAgentDialog.vue";
import * as userAgentsApi from "@/api/userAgents";
import { timeAgo } from "@/utils/datetime";
import { errHtml, escapeHtml } from "@/lib/errors";
import { canRetrySync, lastRun, scheduleSummary, stateBadge, syncMessage } from "@/lib/userAgents";

const props = defineProps({
	agents: { type: Array, default: () => [] },
	maxAgents: { type: Number, default: 5 },
	// The workspace's web browser option: {enabled, max, inUse}.
	browser: { type: Object, default: () => ({ enabled: false, max: 3, inUse: 0 }) },
	loading: { type: Boolean, default: false },
	error: { type: String, default: "" },
});
const emit = defineEmits(["reload", "upsert", "remove"]);

const router = useRouter();

const COLUMNS = [
	{ label: "On", key: "enabled", width: "3.5rem" },
	{ label: "Name", key: "agent_name", width: 2 },
	{ label: "Status", key: "state", width: "6.5rem" },
	{ label: "Schedule", key: "schedule", width: "10rem" },
	{ label: "Last run", key: "last_run", width: "8rem" },
	{ label: "", key: "_actions", width: "13rem", align: "right" },
];

const atCap = computed(() => props.agents.length >= props.maxAgents);

// ── create / edit ────────────────────────────────────────────────────────────
const dialogOpen = ref(false);
const editingName = ref("");
// The live row, so the dialog shows a sync failure as soon as the list learns of it.
const editingAgent = computed(
	() => props.agents.find((a) => a.name === editingName.value) || null
);

function openNew() {
	editingName.value = "";
	dialogOpen.value = true;
}
function openEdit(row) {
	editingName.value = row.name;
	dialogOpen.value = true;
}
// After a create the dialog stays on the new agent: a second Save edits it.
function onSaved(agent) {
	emit("upsert", agent);
	if (dialogOpen.value) editingName.value = agent.name;
}

// ── row actions ──────────────────────────────────────────────────────────────
// One action at a time: Chat and Run now each open a conversation.
const busy = ref("");

async function toggleEnabled(row, value) {
	if (busy.value) return;
	busy.value = row.name + ":enabled";
	try {
		emit("upsert", await userAgentsApi.setUserAgentEnabled(row.name, value));
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		busy.value = "";
	}
}

async function chat(row) {
	if (busy.value) return;
	busy.value = row.name + ":chat";
	try {
		const res = (await userAgentsApi.startUserAgentChat(row.name)) || {};
		if (res.conversation) router.push("/c/" + res.conversation);
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		busy.value = "";
	}
}

async function runNow(row) {
	if (busy.value) return;
	busy.value = row.name + ":run";
	try {
		const res = (await userAgentsApi.runUserAgentNow(row.name)) || {};
		if (res.conversation) router.push("/c/" + res.conversation);
		else emit("reload");
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		busy.value = "";
	}
}

async function retrySync(row) {
	try {
		emit("upsert", await userAgentsApi.retryUserAgentSync(row.name));
	} catch (e) {
		toast.error(errHtml(e));
	}
}

function confirmDelete(row) {
	confirmDialog({
		title: `Delete ${row.agent_name}?`,
		message:
			"Deletes the agent and removes it from your workspace. Past chats with it stay, but you can no longer send messages in them. This can't be undone.",
		onConfirm: async ({ hideDialog }) => {
			try {
				await userAgentsApi.deleteUserAgent(row.name);
				emit("remove", row.name);
				toast.success(`Deleted “${escapeHtml(row.agent_name)}”`);
				hideDialog();
			} catch (e) {
				toast.error(errHtml(e));
			}
		},
	});
}

function menuOptions(row) {
	return [
		...(canRetrySync(row) ? [{ label: "Retry sync", onClick: () => retrySync(row) }] : []),
		{ label: "Delete", onClick: () => confirmDelete(row) },
	];
}

// ── cell text ────────────────────────────────────────────────────────────────
function notReadyReason(row) {
	return row.enabled
		? "Chat opens once the agent has synced. Retry the sync if it failed."
		: "Turn this agent on to chat with it.";
}
function runReason(row) {
	if (!row.ready) return notReadyReason(row).replace("Chat opens", "Run now works");
	if (!row.schedule.prompt) return "Edit the agent and add a prompt for each run first.";
	return "Run now";
}
function lastRunHint(row) {
	const r = lastRun(row);
	return (
		r.message || (r.status === "Started" ? "The last run started." : "The last run failed.")
	);
}
</script>
