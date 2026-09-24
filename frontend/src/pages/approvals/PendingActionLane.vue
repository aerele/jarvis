<template>
	<!-- Pending actions: held File Box writes (PR-2b), new records an unattended
	     File Box run wants to create, and the viewer's own chat cards (PR-3c), each
	     paused on a human decision. Self-contained above the approval rail; hidden
	     while nothing waits. A row expands into its PendingActionDetail, or
	     PendingChatDetail for a chat card. -->
	<section
		v-if="rows.length || error"
		class="border-b bg-surface-gray-1"
		aria-labelledby="held-lane-title"
	>
		<button
			class="flex w-full items-center gap-2 px-5 py-2.5 text-left hover:bg-surface-gray-2"
			:aria-expanded="collapsed ? 'false' : 'true'"
			aria-controls="held-lane-body"
			@click="collapsed = !collapsed"
		>
			<FeatherIcon
				:name="collapsed ? 'chevron-right' : 'chevron-down'"
				class="size-4 shrink-0 text-ink-gray-5"
				aria-hidden="true"
			/>
			<span id="held-lane-title" class="text-sm font-medium text-ink-gray-8">{{
				heading.title
			}}</span>
			<Badge
				v-if="rows.length"
				variant="subtle"
				theme="orange"
				:label="String(rows.length)"
			/>
			<span class="text-xs text-ink-gray-5">{{ heading.source }}</span>
		</button>

		<!-- Capped so the approval rail below and an open detail's buttons stay reachable. -->
		<div
			v-if="!collapsed"
			id="held-lane-body"
			class="flex max-h-[45vh] flex-col divide-y overflow-y-auto border-t"
		>
			<div v-if="error" role="alert" class="flex items-center gap-3 px-5 py-3 text-sm">
				<span class="text-ink-red-5">{{ error }}</span>
				<Button variant="subtle" size="sm" label="Try again" @click="load" />
			</div>
			<div v-for="r in rows" :key="r.name">
				<button
					class="flex w-full items-center gap-3 px-5 py-2.5 text-left"
					:class="
						openName === r.name ? 'bg-surface-selected' : 'hover:bg-surface-gray-2'
					"
					:aria-expanded="openName === r.name ? 'true' : 'false'"
					:aria-controls="'held-detail-' + r.name"
					@click="toggle(r.name)"
				>
					<FeatherIcon
						:name="isChat(r) ? 'message-square' : 'user-plus'"
						class="size-4 shrink-0 text-ink-gray-5"
						aria-hidden="true"
					/>
					<div class="min-w-0 flex-1">
						<div class="truncate text-base text-ink-gray-9">
							{{ r.summary || (isChat(r) ? "Action waiting" : "New record") }}
						</div>
						<div
							v-if="isChat(r)"
							class="mt-0.5 flex min-w-0 items-center gap-2 text-sm text-ink-gray-5"
						>
							<span class="truncate">{{ r.conversation_title || "Chat" }}</span>
							<Tooltip :text="exactDate(r.created_at)">
								<span class="whitespace-nowrap"
									>· Proposed {{ timeAgo(r.created_at) }}</span
								>
							</Tooltip>
						</div>
						<div v-else class="mt-0.5 flex items-center gap-2 text-sm text-ink-gray-5">
							<span>{{ filesWaiting(r.waiters_count) }}</span>
							<span v-if="r.for_user">· dropped by {{ r.for_user }}</span>
							<Tooltip :text="exactDate(r.created_at)">
								<span class="whitespace-nowrap">{{ timeAgo(r.created_at) }}</span>
							</Tooltip>
						</div>
					</div>
					<Badge
						v-if="r.status === 'Executing'"
						variant="subtle"
						theme="blue"
						:label="isChat(r) ? 'Running…' : 'Creating…'"
					/>
				</button>
				<PendingChatDetail
					v-if="openName === r.name && isChat(r)"
					:id="'held-detail-' + r.name"
					:name="r.name"
					@decided="onDecided(r.name)"
				/>
				<PendingActionDetail
					v-else-if="openName === r.name"
					:id="'held-detail-' + r.name"
					:name="r.name"
					@decided="onDecided(r.name)"
				/>
			</div>
		</div>
	</section>
</template>

<script setup>
// The Approval Board's pending-actions lane (unified pending action, PR-2b/PR-3c).
// Rows come from list_pending_actions_lane: held writes (owner, or every user's for
// a System Manager) and the viewer's own chat cards (never another user's, D1); the
// AR rail below is untouched. Refetches (debounced) on the realtime frames that
// mean "something new is waiting" and when the tab becomes visible.
import { ref, computed, inject, watch, onMounted, onBeforeUnmount } from "vue";
import { useRoute } from "vue-router";
import { Badge, Button, FeatherIcon, Tooltip } from "frappe-ui";
import PendingActionDetail from "@/pages/approvals/PendingActionDetail.vue";
import PendingChatDetail from "@/pages/approvals/PendingChatDetail.vue";
import { listPendingActionsLane } from "@/api/approvals";
import { useShellStore } from "@/stores/shell";
import { errMessage } from "@/lib/errors";
import { filesWaiting } from "@/lib/heldActions";
import { laneHeading } from "@/lib/chatCardActions";
import { timeAgo, exactDate } from "@/utils/datetime";

const REFRESH_KINDS = new Set([
	"action:pending",
	"approval:new",
	"action:confirmed",
	"action:settled",
]);
const REFRESH_DEBOUNCE_MS = 1000;

const route = useRoute();
const store = useShellStore();
const socket = inject("$socket", null);

const rows = ref([]);
const error = ref("");
const heading = computed(() => laneHeading(rows.value));
const isChat = (r) => r.kind === "chat";
const collapsed = ref(false);
const openName = ref("");
// ?held=<name> (the File Box "Needs approval" link) opens that row once it loads.
let wanted = (route && typeof route.query.held === "string" && route.query.held) || "";

let req = 0;
async function load() {
	const id = ++req;
	try {
		const res = (await listPendingActionsLane()) || {};
		if (id !== req) return;
		rows.value = Array.isArray(res.rows) ? res.rows : [];
		error.value = "";
		if (wanted && rows.value.some((r) => r.name === wanted)) {
			openName.value = wanted;
			collapsed.value = false;
			wanted = "";
		}
		if (openName.value && !rows.value.some((r) => r.name === openName.value))
			openName.value = "";
	} catch (e) {
		if (id !== req) return;
		error.value = errMessage(e, "Waiting approvals could not be loaded.");
	}
}

function toggle(name) {
	openName.value = openName.value === name ? "" : name;
}

function onDecided(name) {
	rows.value = rows.value.filter((r) => r.name !== name);
	if (openName.value === name) openName.value = "";
	store.refreshApprovalsCount();
	load();
}

let timer = null;
function onEvent(p) {
	if (!p || !REFRESH_KINDS.has(p.kind) || timer) return;
	timer = setTimeout(() => {
		timer = null;
		load();
	}, REFRESH_DEBOUNCE_MS);
}
function onVisibility() {
	if (document.visibilityState === "visible") load();
}

watch(
	() => route && route.query.held,
	(held) => {
		if (typeof held !== "string" || !held) return;
		wanted = held;
		load();
	}
);

onMounted(() => {
	load();
	socket && socket.on && socket.on("jarvis:event", onEvent);
	document.addEventListener("visibilitychange", onVisibility);
});
onBeforeUnmount(() => {
	socket && socket.off && socket.off("jarvis:event", onEvent);
	document.removeEventListener("visibilitychange", onVisibility);
	clearTimeout(timer);
});

defineExpose({ load });
</script>
