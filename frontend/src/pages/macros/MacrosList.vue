<template>
	<div class="flex h-full flex-col overflow-hidden">
		<TabBar
			class="shrink-0"
			:tabs="TABS"
			:model-value="activeTab"
			@update:model-value="onTab"
		/>

		<!-- ============ Macros tab ============ -->
		<ListPage
			v-if="activeTab === 'macros'"
			class="min-h-0 flex-1"
			:breadcrumbs="[{ label: 'Macros', route: { name: 'MacrosList' } }]"
			:columns="columns"
			:rows="rows"
			:loading="loading"
			:total="total"
			:has-more="hasMore"
			:quick-filters="quickFilters"
			:filter-state="filterState"
			:filters="filters"
			:sort-options="sortOptions"
			:sort="sort"
			:page-length="pageLength"
			:default-sort="DEFAULT_SORT"
			:selectable="true"
			:get-row-route="getRowRoute"
			storage-key="macros"
			:empty-state="{
				icon: 'layers',
				title: 'No Macros Found',
				description:
					'Turn a chat into a repeatable macro with Save as macro, or create one here with New Macro.',
			}"
			@update:filters="setFilters"
			@update:filter-clauses="setClauses"
			@request-filter-schema="requestSchema"
			@dismiss-filter-notice="dismissFilterNotice"
			@update:sort="(s) => setSort(s.field, s.dir)"
			@update:page-length="(v) => (pageLength = v)"
			@load-more="loadMore"
			@refresh="resetLoad"
		>
			<template #right-header>
				<Button
					variant="solid"
					label="New Macro"
					iconLeft="plus"
					@click="router.push({ name: 'MacroNew' })"
				/>
			</template>

			<template #cell-step_count="{ row }">
				<div class="flex w-full items-center justify-center text-base text-ink-gray-7">
					{{ row.step_count || 0 }} {{ (row.step_count || 0) === 1 ? "step" : "steps" }}
				</div>
			</template>

			<template #cell-has_summary="{ row }">
				<Badge
					v-if="row.merge_status === 'pending'"
					variant="subtle"
					theme="orange"
					label="Summarizing…"
				/>
				<Badge
					v-else-if="row.has_summary"
					variant="subtle"
					theme="gray"
					label="Summarized"
				/>
				<span v-else class="text-base text-ink-gray-4">-</span>
			</template>

			<template #cell-schedule="{ row }">
				<div v-if="row.schedule_enabled" class="truncate text-base">
					{{ scheduleLabel(row) }}
				</div>
				<span v-else class="text-base text-ink-gray-4">-</span>
			</template>

			<template #cell-last_run_at="{ row }">
				<Tooltip v-if="row.last_run_at" :text="exactDate(row.last_run_at)">
					<div class="truncate text-base">{{ timeAgo(row.last_run_at) }}</div>
				</Tooltip>
				<span v-else class="text-base text-ink-gray-4">-</span>
			</template>

			<template #cell-next_run_at="{ row }">
				<Tooltip v-if="row.next_run_at" :text="exactDate(row.next_run_at)">
					<div class="truncate text-base">{{ timeAgo(row.next_run_at) }}</div>
				</Tooltip>
				<span v-else class="text-base text-ink-gray-4">-</span>
			</template>

			<template #cell-_run="{ row }">
				<div class="flex w-full items-center justify-end" @click.stop.prevent>
					<Button
						variant="ghost"
						icon="play"
						:loading="runningRow === row.name"
						:disabled="row.merge_status === 'pending' || !!runningRow"
						:tooltip="
							row.merge_status === 'pending'
								? 'Summarizing… - Run unlocks when the summary is ready'
								: 'Run'
						"
						@click="runRow(row)"
					/>
				</div>
			</template>

			<template #select-actions="{ selections, unselectAll }">
				<Dropdown
					:options="[
						{ label: 'Delete', onClick: () => bulkDelete(selections, unselectAll) },
					]"
				>
					<Button icon="more-horizontal" variant="ghost" />
				</Dropdown>
			</template>
		</ListPage>

		<!-- ============ Runs tab ============ -->
		<RunsTab v-else class="min-h-0 flex-1" />
	</div>
</template>

<script setup>
// Macros list - DESIGN-V3 §5.9: TabBar (Macros | Runs) synced to /macros vs
// /macros/runs, envelope list with enabled/schedule quick filters, summary +
// schedule badge cells, inline ghost Run cell (gated while summarizing), bulk
// delete (incl. run history) and macro:merged live refresh.
import { ref, computed, inject, onMounted, onBeforeUnmount } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Button, Badge, Tooltip, Dropdown, toast, confirmDialog } from "frappe-ui";
import ListPage from "@/components/list/ListPage.vue";
import TabBar from "@/components/list/TabBar.vue";
import { useListPage } from "@/composables/useListPage";
import RunsTab from "./RunsTab.vue";
import { timeAgo, exactDate } from "@/utils/datetime";
import * as api from "@/api";
import * as apiMacros from "@/api/macros";

const props = defineProps({
	tab: { type: String, default: "macros" }, // 'runs' on /macros/runs (§9)
});

const router = useRouter();
// /macros and /macros/runs are two routes over one component, so the `fv2`
// clause payload carries its view key and the Runs tab's URL never applies the
// Macros tab's filters (judgment C08-7).
const route = useRoute();
const socket = inject("$socket");

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

// ── tabs (synced to the route, D32-friendly: real URLs per tab) ──────────────
const TABS = [
	{ label: "Macros", value: "macros" },
	{ label: "Runs", value: "runs" },
];
const activeTab = computed(() => (props.tab === "runs" ? "runs" : "macros"));

function onTab(v) {
	if (v === activeTab.value) return;
	// Carry the query across. A named location without one resolves to a URL with
	// NO query at all, so a bare `router.push({name:'MacroRuns'})` silently threw
	// away the `fv2` filter payload — switch to Runs and back and every filter was
	// gone. The tab is a different route over the same list state, not a reset.
	router.push({
		name: v === "runs" ? "MacroRuns" : "MacrosList",
		query: { ...route.query },
	});
}

// ── list config ──────────────────────────────────────────────────────────────
const ENABLED_OPTIONS = [
	{ label: "All", value: "" },
	{ label: "Enabled", value: "1" },
	{ label: "Draft", value: "0" },
];
const SCHEDULE_OPTIONS = [
	{ label: "All", value: "" },
	{ label: "Scheduled", value: "1" },
	{ label: "Manual", value: "0" },
];

const columns = [
	{ label: "Name", key: "macro_name", width: 2 },
	{ label: "Steps", key: "step_count", width: "6rem", align: "center" },
	{ label: "Summary", key: "has_summary", width: "8rem" },
	{ label: "Schedule", key: "schedule", width: "9rem" },
	{ label: "Last run", key: "last_run_at", width: "8rem" },
	{ label: "Next run", key: "next_run_at", width: "8rem" },
	{ label: "", key: "_run", width: "4rem", align: "right" },
];

// search rides the quick-filter strip (same pattern as SkillsList): it lives
// in the filters object for a controlled input, and fetchFn moves it onto the
// envelope's `search` param (the backend throws on unknown filter keys).
const quickFilters = [
	{ key: "search", label: "Search macros", type: "text" },
	{ key: "enabled", label: "Status", type: "select", options: ENABLED_OPTIONS },
	{ key: "schedule_enabled", label: "Schedule", type: "select", options: SCHEDULE_OPTIONS },
];
// plan 08 wave 1: the curated `filterDefs` array is GONE as the filter catalog.
// The Filter panel is generated from the server schema for view_key "macros",
// which is every readable Jarvis Macro field plus the standard fields plus the
// Jarvis Macro Step child fields (compiled as one EXISTS, deviation D4) —
// `schedule_frequency`, which used to need this array, is just one of them now.
// Both remaining quick controls map 1:1 onto a canonical field, so both stay in
// sync with the panel in either direction.
const QUICK_CLAUSES = { enabled: "enabled", schedule_enabled: "schedule_enabled" };

const sortOptions = [
	{ label: "Updated", value: "modified" },
	{ label: "Name", value: "macro_name" },
	{ label: "Last run", value: "last_run_at" },
	{ label: "Next run", value: "next_run_at" },
];
const DEFAULT_SORT = { field: "modified", dir: "desc" };

const {
	rows,
	total,
	hasMore,
	loading,
	filters,
	setFilters,
	sort,
	setSort,
	pageLength,
	resetLoad,
	loadMore,
	refreshKeep,
	filterState,
	setClauses,
	requestSchema,
	dismissFilterNotice,
} = useListPage({
	fetchFn: (p) => {
		const { search: q, ...rest } = p.filters || {};
		return api.listMacrosPage({ ...p, search: q || p.search || "", filters: rest });
	},
	defaultSort: DEFAULT_SORT,
	storageKey: "macros",
	viewKey: "macros",
	quickClauses: QUICK_CLAUSES,
	// The view's identity (it mirrors this view's list_registry entry), NOT a
	// field catalog — that only ever comes from the server. It lets a quick
	// filter become a canonical, shareable clause before the catalog is fetched.
	rootDoctype: "Jarvis Macro",
	route,
	router,
});

function getRowRoute(row) {
	return { name: "MacroDetail", params: { id: row.name } };
}

// ── inline Run (D17: ghost interactive cell) ─────────────────────────────────
const runningRow = ref("");

async function runRow(row) {
	if (runningRow.value || row.merge_status === "pending") return;
	runningRow.value = row.name;
	try {
		const res = await api.runMacro(row.name);
		const data = (res && res.data) || res || {};
		toast.success(`Running “${row.macro_name || row.name}”`);
		// hand off to the chat - the live macro banner is ChatView's machinery
		if (data.conversation) router.push("/c/" + data.conversation);
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		runningRow.value = "";
	}
}

// ── bulk delete (run history goes too - server side, per row) ────────────────
function bulkDelete(selections, unselectAll) {
	const names = Array.from(selections || []);
	if (!names.length) return;
	confirmDialog({
		title: `Delete ${names.length} macro${names.length === 1 ? "" : "s"}?`,
		message: "Deletes the selected macros AND their run history. This can't be undone.",
		onConfirm: async ({ hideDialog }) => {
			try {
				const res = (await apiMacros.deleteMacrosBulk(names)) || {};
				const skipped = res.skipped || [];
				const deleted = res.deleted != null ? res.deleted : names.length - skipped.length;
				if (skipped.length) {
					const reasons = [...new Set(skipped.map((s) => s.reason || "skipped"))].join(
						", "
					);
					toast.create({
						message: `Deleted ${deleted} (skipped ${skipped.length}: ${reasons})`,
						type: "info",
					});
				} else {
					toast.success(`Deleted ${deleted} macro${deleted === 1 ? "" : "s"}`);
				}
				unselectAll();
				hideDialog();
				resetLoad();
			} catch (e) {
				toast.error(errMsg(e));
			}
		},
	});
}

// ── live merge updates: the Run gate flips when the summary lands ────────────
function onEvent(p) {
	if (!p || p.kind !== "macro:merged") return;
	refreshKeep();
	if (p.status === "ready") {
		toast.success(`Summary ready - “${p.macro_name || "macro"}” now runs as one prompt.`);
	} else {
		toast.create({
			message: `“${p.macro_name || "Macro"}” keeps its step sequence (couldn't summarize).`,
			type: "info",
		});
	}
}

onMounted(() => {
	socket && socket.on && socket.on("jarvis:event", onEvent);
});
onBeforeUnmount(() => {
	socket && socket.off && socket.off("jarvis:event", onEvent);
});

// ── cell helpers ─────────────────────────────────────────────────────────────
function scheduleLabel(row) {
	const freq = row.schedule_frequency || "scheduled";
	const label = freq.charAt(0).toUpperCase() + freq.slice(1);
	const t = toHHMM(row.schedule_time);
	return t ? `${label} · ${t}` : label;
}
function toHHMM(t) {
	const m = /^(\d{1,2}):(\d{2})/.exec(String(t || ""));
	return m ? `${m[1].padStart(2, "0")}:${m[2]}` : "";
}
</script>
