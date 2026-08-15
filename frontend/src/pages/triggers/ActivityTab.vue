<template>
	<div class="flex h-full flex-col overflow-hidden">
		<ListPage
			class="min-h-0 flex-1"
			:breadcrumbs="[{ label: 'Triggers', route: { name: 'TriggersPage' } }]"
			:columns="columns"
			:rows="rows"
			:loading="loading"
			:error="error"
			:total="total"
			:has-more="hasMore"
			:quick-filters="quickFilters"
			:filter-defs="filterDefs"
			:filters="filters"
			:sort-options="sortOptions"
			:sort="sort"
			:page-length="pageLength"
			:default-sort="DEFAULT_SORT"
			:selectable="false"
			:on-row-click="openRow"
			storage-key="trigger-activity"
			:empty-state="{
				icon: 'activity',
				title: 'No trigger activity yet',
				description: 'Runs appear here when an enabled trigger fires on a document event.',
			}"
			@update:filters="setFilters"
			@update:sort="(s) => setSort(s.field, s.dir)"
			@update:page-length="(v) => (pageLength = v)"
			@load-more="loadMore"
			@refresh="reload"
		>
			<!-- admin stat strip (activity_stats returns {} for everyone else) -->
			<template v-if="statsLine" #banner>
				<div class="pb-1 text-sm text-ink-gray-5">{{ statsLine }}</div>
			</template>

			<template #cell-status="{ row }">
				<Badge
					variant="subtle"
					:theme="STATUS_THEME[row.status] || 'gray'"
					:label="row.status || '-'"
				/>
			</template>

			<!-- custom #cell slots bypass ListRowItem's Tooltip wrapper, so each
			     truncating cell carries a native title to keep the full value
			     recoverable on hover -->
			<template #cell-trigger_label="{ row }">
				<div
					class="truncate text-base font-medium text-ink-gray-8"
					:title="row.trigger_label || row.trigger"
				>
					{{ row.trigger_label || row.trigger }}
				</div>
			</template>

			<template #cell-target="{ row }">
				<div class="flex w-full min-w-0 items-center gap-1">
					<span
						class="truncate text-base text-ink-gray-7"
						:title="row.target_doctype + ' · ' + row.target_docname"
					>
						{{ row.target_doctype }} · {{ row.target_docname }}
					</span>
					<a
						v-if="row.target_doctype && row.target_docname"
						:href="deskUrl(row)"
						target="_blank"
						rel="noopener"
						class="shrink-0 text-ink-gray-5 hover:text-ink-gray-8"
						:aria-label="
							'Open ' + row.target_doctype + ' ' + row.target_docname + ' in Desk'
						"
						@click.stop
					>
						<FeatherIcon name="external-link" class="size-3.5" />
					</a>
				</div>
			</template>

			<template #cell-doc_event="{ row }">
				<div class="truncate text-base text-ink-gray-7" :title="eventLabel(row.doc_event)">
					{{ eventLabel(row.doc_event) }}
				</div>
			</template>

			<template #cell-action_type="{ row }">
				<Badge
					v-if="row.action_type === 'Script'"
					variant="subtle"
					theme="blue"
					label="Script"
				/>
				<span
					v-else-if="row.action_type === 'LLM'"
					class="inline-flex h-5 select-none items-center whitespace-nowrap rounded-full bg-surface-violet-1 px-1.5 text-xs text-ink-violet-1"
				>
					LLM
				</span>
				<span v-else class="text-base text-ink-gray-4">-</span>
			</template>

			<template #cell-summary="{ row }">
				<div class="truncate text-base text-ink-gray-7" :title="row.summary || undefined">
					{{ row.summary || "-" }}
				</div>
			</template>

			<template #cell-creation="{ row }">
				<Tooltip v-if="row.creation" :text="exactDate(row.creation)">
					<div class="truncate text-base">{{ timeAgo(row.creation) }}</div>
				</Tooltip>
				<span v-else class="text-base text-ink-gray-4">-</span>
			</template>
		</ListPage>

		<ActivityDetailDialog v-model="detailOpen" :row="detailRow" :caps="caps" />
	</div>
</template>

<script setup>
// ActivityTab - /triggers#activity: ListPage + useListPage against
// triggers_api.list_activity_page (search · status quick filters ·
// status/action/event/daterange FilterButton defs · Load-More pagination).
// Row click opens the detail Dialog fed from the row itself (there is no
// per-row fetch endpoint; the mono `detail` block renders whenever the
// backend ships that field on rows). Realtime: "trigger:activity" frames silently
// refresh page 1 when the viewer sits unfiltered on it (the MacrosList
// live-refresh manner); admin stat strip from activity_stats.
import { ref, computed, watch, inject, onMounted, onBeforeUnmount } from "vue";
import { Badge, FeatherIcon, Tooltip } from "frappe-ui";
import ListPage from "@/components/list/ListPage.vue";
import { useListPage } from "@/composables/useListPage";
import { timeAgo, exactDate } from "@/utils/datetime";
import ActivityDetailDialog from "./ActivityDetailDialog.vue";
import * as apiTriggers from "@/api/triggers";

const props = defineProps({
	caps: { type: Object, default: () => ({}) }, // get_triggers_caps payload
	initialTrigger: { type: String, default: "" }, // ?trigger= deep link
});

const socket = inject("$socket", null);

// ── list config ──────────────────────────────────────────────────────────────
const STATUS_THEME = { Success: "green", Failed: "red", Blocked: "orange", Skipped: "gray" };
const STATUS_OPTIONS = [
	{ label: "All statuses", value: "" },
	{ label: "Success", value: "Success" },
	{ label: "Failed", value: "Failed" },
	{ label: "Blocked", value: "Blocked" },
	{ label: "Skipped", value: "Skipped" },
];
const ACTION_OPTIONS = [
	{ label: "All actions", value: "" },
	{ label: "Script", value: "Script" },
	{ label: "LLM", value: "LLM" },
];

// Event gets 10.5rem so the longest label ("Before Save (blockable)") shows
// its qualifier instead of clipping.
const columns = [
	{ label: "Status", key: "status", width: "6rem" },
	{ label: "Trigger", key: "trigger_label", width: 2 },
	{ label: "Target", key: "target", width: 2 },
	{ label: "Event", key: "doc_event", width: "10.5rem" },
	{ label: "Action", key: "action_type", width: "5rem" },
	{ label: "Summary", key: "summary", width: 2 },
	{ label: "When", key: "creation", width: "7rem" },
];

// search + the text-y filters ride the quick strip (FilterButton only builds
// select/daterange rows); trigger id is filterable for the ?trigger= deep link.
const quickFilters = computed(() => [
	{ key: "search", label: "Search activity", type: "text" },
	{ key: "status", label: "Status", type: "select", options: STATUS_OPTIONS },
	{ key: "target_doctype", label: "DocType (exact)", type: "text" },
	{ key: "trigger", label: "Trigger id", type: "text" },
]);
const filterDefs = computed(() => [
	{ key: "status", label: "Status", type: "select", options: STATUS_OPTIONS },
	{ key: "action_type", label: "Action", type: "select", options: ACTION_OPTIONS },
	{ key: "doc_event", label: "Event", type: "select", options: eventOptions.value },
	{ key: "daterange", label: "Date", type: "daterange" },
]);

// backend _ACTIVITY_SORTABLE whitelist (unknown fields THROW):
// creation · status · target_doctype
const sortOptions = [
	{ label: "When", value: "creation" },
	{ label: "Status", value: "status" },
	{ label: "DocType", value: "target_doctype" },
];
const DEFAULT_SORT = { field: "creation", dir: "desc" };

const {
	rows,
	total,
	hasMore,
	loading,
	error,
	filters,
	setFilters,
	sort,
	setSort,
	pageLength,
	resetLoad,
	loadMore,
	refreshKeep,
} = useListPage({
	fetchFn: (p) => {
		const { search: q, ...rest } = p.filters || {};
		return apiTriggers.listActivityPage({ ...p, search: q || p.search || "", filters: rest });
	},
	defaultSort: DEFAULT_SORT,
	storageKey: "trigger-activity",
	initialFilters: props.initialTrigger ? { trigger: props.initialTrigger } : {},
});

function reload() {
	resetLoad();
	loadStats();
}

// ── caps.events → labels ─────────────────────────────────────────────────────
const eventOptions = computed(() => [
	{ label: "All events", value: "" },
	...(props.caps.events || []).map((e) => ({ label: e.label, value: e.value })),
]);
function eventLabel(value) {
	const hit = (props.caps.events || []).find((e) => e.value === value);
	return (hit && hit.label) || value || "-";
}

// Desk deep link - the ApprovalsBoard refUrl slug recipe
function deskUrl(row) {
	const dt = String(row.target_doctype || "")
		.toLowerCase()
		.replace(/ /g, "-");
	return `/app/${dt}/${encodeURIComponent(row.target_docname || "")}`;
}

// ── row detail dialog ────────────────────────────────────────────────────────
const detailOpen = ref(false);
const detailRow = ref(null);
function openRow(row) {
	detailRow.value = row;
	detailOpen.value = true;
}

// ── admin stat strip ─────────────────────────────────────────────────────────
const stats = ref(null);
const statsLine = computed(() => {
	// activity_stats data: {last_24h: {Success, Failed, Blocked, Skipped},
	// total_rows} for admins, {} for others (by_status accepted defensively -
	// the draft contract's name for the same map)
	const s = stats.value || {};
	const by = s.last_24h || s.by_status;
	if (!by || !Object.keys(by).length) return "";
	return `Last 24h: ${by.Success || 0} ok · ${by.Failed || 0} failed`;
});
async function loadStats() {
	if (!props.caps.can_manage) return;
	try {
		stats.value = (await apiTriggers.activityStats()) || null;
	} catch (e) {
		// best-effort strip; the list is the real surface
	}
}
// the caps probe resolves async - load stats once admin rights are known
watch(
	() => props.caps.can_manage,
	(v) => {
		if (v) loadStats();
	},
	{ immediate: true }
);

// ── realtime: new activity rows land silently on an unfiltered page 1 ────────
// Coalesced: a bulk save fires one trigger:activity frame per doc — the first
// frame arms a 1.5s timer and the rest of the burst is absorbed into that one
// list+stats refresh. The page-1/unfiltered guard is evaluated when the timer
// fires (not when the frame lands), and stats only refetch for admins — the
// only viewers whose stat strip renders.
const unfilteredFirstPage = computed(
	() => !Object.keys(filters).length && rows.value.length <= pageLength.value
);
let rtTimer = null;
function onEvent(p) {
	if (!p || p.kind !== "trigger:activity") return;
	if (rtTimer) return;
	rtTimer = setTimeout(() => {
		rtTimer = null;
		if (unfilteredFirstPage.value) refreshKeep();
		if (props.caps.can_manage) loadStats();
	}, 1500);
}

onMounted(() => {
	socket && socket.on && socket.on("jarvis:event", onEvent);
});
onBeforeUnmount(() => {
	socket && socket.off && socket.off("jarvis:event", onEvent);
	clearTimeout(rtTimer);
});
</script>
