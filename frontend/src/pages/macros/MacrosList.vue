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
			:error="error"
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

			<!-- What an admin did that left no row to show it: a macro of this user
			     an admin deleted. One line per notice, until dismissed. -->
			<template v-if="notices.length" #banner>
				<div class="flex flex-col gap-2 pt-3" data-testid="admin-notices">
					<div
						v-for="n in notices"
						:key="n.name"
						class="jv-macro-notice flex items-center justify-between gap-3 rounded-md bg-surface-amber-2 px-3 py-2"
						role="status"
						:data-notice="n.name"
					>
						<span class="text-sm text-ink-gray-8">{{ n.message }}</span>
						<Button
							variant="ghost"
							size="sm"
							label="Dismiss"
							:loading="dismissing === n.name"
							:disabled="!!dismissing"
							:aria-label="`Dismiss: ${n.message}`"
							@click="dismiss(n)"
						/>
					</div>
				</div>
			</template>

			<template #cell-macro_name="{ row }">
				<div class="flex items-center gap-2">
					<span class="truncate text-base font-medium text-ink-gray-9">{{
						row.macro_name
					}}</span>
					<Tooltip
						v-if="row.skip_confirmation"
						text="Armed: this macro's runs execute writes without a confirmation card (admin-set)."
					>
						<Badge variant="subtle" theme="orange" label="Armed" />
					</Tooltip>
					<Tooltip v-if="macroHold(row)" :text="holdMessage(macroHold(row))">
						<Badge
							class="jv-macro-held"
							variant="subtle"
							theme="red"
							label="On hold"
						/>
						<!-- The tooltip shows on hover only: the reason is in the text too. -->
						<span class="jv-macro-held-reason sr-only">{{
							holdMessage(macroHold(row))
						}}</span>
					</Tooltip>
				</div>
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
				<Tooltip v-if="lastRunCell(row)" :text="lastRunCell(row).hint">
					<div class="truncate text-base" :class="lastRunCell(row).class">
						{{ lastRunCell(row).text }}
					</div>
				</Tooltip>
				<span v-else class="text-base text-ink-gray-4">-</span>
			</template>

			<template #cell-next_run_at="{ row }">
				<Tooltip v-if="nextRunCell(row)" :text="nextRunCell(row).hint">
					<div class="truncate text-base" :class="nextRunCell(row).class">
						{{ nextRunCell(row).text }}
					</div>
				</Tooltip>
				<span v-else class="text-base text-ink-gray-4">-</span>
			</template>

			<template #cell-_run="{ row }">
				<div class="flex w-full items-center justify-end" @click.stop.prevent>
					<Button
						variant="ghost"
						icon="play"
						:loading="runningRow === row.name"
						:disabled="
							!!macroHold(row) || row.merge_status === 'pending' || !!runningRow
						"
						:tooltip="
							macroHold(row)
								? holdMessage(macroHold(row))
								: row.merge_status === 'pending'
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
import { macrosListFetch } from "@/pages/list/listFetchers";
import RunsTab from "./RunsTab.vue";
import { timeAgo, exactDate, toLocalMs } from "@/utils/datetime";
import { scheduleLabel } from "./scheduleLabel";
import { LAST_RUN_TONE, NEXT_RUN_TONE, enabledLabel } from "./runDisplay";
import { nextRunCell as describeNextRun } from "@/lib/macroSchedule";
import {
	lastRunCell as describeLastRun,
	deleteWarning,
	bulkDeleteToast,
	deleteGuard,
	isLiveRun,
} from "@/lib/macroRunOutcome";
import * as api from "@/api";
import * as apiMacros from "@/api/macros";
import { errHtml, escapeHtml } from "@/lib/errors";
import { macroHold, holdMessage } from "@/lib/macroHold";

const props = defineProps({
	tab: { type: String, default: "macros" }, // 'runs' on /macros/runs (§9)
});

const router = useRouter();
// /macros and /macros/runs are two routes over one component, so the `fv2`
// clause payload carries its view key and the Runs tab's URL never applies the
// Macros tab's filters (judgment C08-7).
const route = useRoute();
const socket = inject("$socket");

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
// A select-type quick filter renders with no label of its own (ListPage), so
// the "everything" option has to say WHAT it is all of: two controls side by
// side both reading "All" could not be told apart.
const ENABLED_OPTIONS = [
	{ label: "All statuses", value: "" },
	{ label: enabledLabel(true), value: "1" },
	{ label: enabledLabel(false), value: "0" },
];
const SCHEDULE_OPTIONS = [
	{ label: "All schedules", value: "" },
	{ label: "Scheduled", value: "1" },
	{ label: "Manual", value: "0" },
];

const columns = [
	{ label: "Name", key: "macro_name", width: 2 },
	{ label: "Steps", key: "step_count", width: "6rem", align: "center" },
	{ label: "Summary", key: "has_summary", width: "8rem" },
	{ label: "Schedule", key: "schedule", width: "9rem" },
	{ label: "Last run", key: "last_run_at", width: "8rem" },
	// Wide enough for the retry cell ("Retry in 44 minutes") without an ellipsis.
	{ label: "Next run", key: "next_run_at", width: "10rem" },
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

const notices = ref([]);
const dismissing = ref("");

const {
	rows,
	total,
	hasMore,
	loading,
	// Passed to ListPage so a load that FAILED shows its error state; without
	// it a failure fell through to the empty state, "No Macros Found".
	error,
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
	fetchFn: macrosListFetch,
	onEnvelope: keepNotices,
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

// ── an admin's notices (a macro of this user was deleted by an admin) ────────
// They ride the list's own answer (`notices`, additive to the envelope), so they
// arrive with every load and need no request of their own. Dismissing marks them
// read on the server; the line goes once the server says so. (`notices` is
// declared above useListPage, which may load at once.)

function keepNotices(res) {
	if (res && Array.isArray(res.notices)) notices.value = res.notices;
}

async function dismiss(notice) {
	if (dismissing.value) return;
	dismissing.value = notice.name;
	try {
		await apiMacros.dismissMacroNotices([notice.name]);
		notices.value = notices.value.filter((n) => n.name !== notice.name);
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		dismissing.value = "";
	}
}

function getRowRoute(row) {
	return { name: "MacroDetail", params: { id: row.name } };
}

// ── inline Run (D17: ghost interactive cell) ─────────────────────────────────
const runningRow = ref("");

async function runRow(row) {
	if (runningRow.value || macroHold(row) || row.merge_status === "pending") return;
	runningRow.value = row.name;
	try {
		const res = await api.runMacro(row.name);
		const data = (res && res.data) || res || {};
		// A toast renders its message as HTML (v-html): the name goes in escaped,
		// here and in the two summary toasts below.
		toast.success(`Running “${escapeHtml(row.macro_name || row.name)}”`);
		// hand off to the chat - the live macro banner is ChatView's machinery
		if (data.conversation) router.push("/c/" + data.conversation);
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		runningRow.value = "";
	}
}

// ── bulk delete (run history goes too - server side, per row) ────────────────
// One delete at a time: the confirmation's own button cannot be told to wait.
const deleting = deleteGuard(toast);

function bulkDelete(selections, unselectAll) {
	const names = Array.from(selections || []);
	if (!names.length || deleting.isBusy()) return;
	// A macro that is running is stopped by the delete (server side). Said here for
	// the selected rows this list knows to have a live run (kept current by a run
	// ending or starting, `onEvent`); the toast reports the rest afterwards.
	const rowOf = new Map((rows.value || []).map((r) => [r.name, r]));
	const running = deleteWarning(names.map((n) => (rowOf.get(n) || {}).last_run));
	confirmDialog({
		title: `Delete ${names.length} macro${names.length === 1 ? "" : "s"}?`,
		message: `${
			running ? `${running} ` : ""
		}Deletes the selected macros AND their run history. This can't be undone.`,
		onConfirm: ({ hideDialog }) => {
			// Closed at once: each live run is stopped first, which can take seconds,
			// and the dialog would sit there with a Confirm button that still works.
			hideDialog();
			return deleting.run(
				running ? "Stopping runs and deleting macros..." : "Deleting macros...",
				async () => {
					try {
						const res = (await apiMacros.deleteMacrosBulk(names)) || {};
						// The toast renders HTML: every macro name goes in escaped.
						const said = bulkDeleteToast(res, names.length, {
							escape: escapeHtml,
							titleOf: (n) => (rowOf.get(n) || {}).macro_name,
						});
						if (said.type === "success") toast.success(said.message);
						else toast.create(said);
						unselectAll();
						resetLoad();
					} catch (e) {
						toast.error(errHtml(e));
					}
				}
			);
		},
	});
}

// ── live merge updates: the Run gate flips when the summary lands ────────────
function onEvent(p) {
	// A run ended: the Last run cell of that macro is now out of date.
	if (p && p.kind === "macro:done") return refreshKeep();
	// A run STARTED that this list has not heard of (the scheduler, another tab):
	// the cell, and the delete confirmation, should know. Once per run: the later
	// steps of the same run find the row already live.
	if (p && p.kind === "macro:progress") {
		const row = (rows.value || []).find((r) => r.name === p.macro);
		return row && !isLiveRun(row.last_run) ? refreshKeep() : undefined;
	}
	if (!p || p.kind !== "macro:merged") return;
	refreshKeep();
	if (p.status === "ready") {
		toast.success(
			`Summary ready - “${escapeHtml(p.macro_name || "macro")}” now runs as one prompt.`
		);
	} else {
		toast.create({
			message: `“${escapeHtml(
				p.macro_name || "Macro"
			)}” keeps its step sequence (couldn't summarize).`,
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
// What the "Next run" cell says, or null for the "-" placeholder. A slot that has
// passed is "Due now" / "Overdue", never the bare relative time ("5 minutes ago").
// The wording and the states live in lib/macroSchedule's nextRunCell (unit-tested,
// imported here as describeNextRun); this wrapper only feeds it the row and maps its
// tone to a colour (./runDisplay, shared with the admin's pane).

// What the "Last run" cell says, or null for the "-" placeholder. It used to print
// a bare time, and only for scheduled runs, so a macro whose last run failed looked
// the same as one that worked. The decision lives in lib/macroRunOutcome.js; this
// wrapper feeds it the row. A server that sends no `last_run` yet keeps the old cell.
function lastRunCell(row) {
	const at = row.last_run && (row.last_run.finished_at || row.last_run.started_at);
	const cell = describeLastRun({
		lastRun: row.last_run,
		when: at ? exactDate(at) : "",
		relative: at ? timeAgo(at) : "",
		legacy: row.last_run_at
			? { when: exactDate(row.last_run_at), relative: timeAgo(row.last_run_at) }
			: null,
	});
	return cell && { ...cell, class: LAST_RUN_TONE[cell.tone] || "" };
}
function nextRunCell(row) {
	const cell = describeNextRun({
		scheduleEnabled: !!row.schedule_enabled,
		enabled: !!row.enabled,
		nextRunMs: toLocalMs(row.next_run_at),
		nowMs: Date.now(),
		when: exactDate(row.next_run_at),
		relative: timeAgo(row.next_run_at),
		isRetry: !!row.next_run_is_retry,
	});
	return cell && { ...cell, class: NEXT_RUN_TONE[cell.tone] || "" };
}
</script>
