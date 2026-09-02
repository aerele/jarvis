<template>
	<div class="flex min-h-0 flex-1 flex-col overflow-hidden">
		<!-- toolbar: search · status facet · Refresh (Approval-Board pattern) -->
		<div class="flex items-center justify-between gap-2 border-b px-5 py-3">
			<div class="flex flex-1 items-center gap-2 overflow-x-auto py-0.5">
				<div class="w-60 shrink-0">
					<FormControl
						type="text"
						placeholder="Search runs"
						:modelValue="search"
						@update:modelValue="(v) => (search = v)"
					/>
				</div>
				<div class="w-40 shrink-0">
					<FormControl
						type="select"
						:options="STATUS_OPTIONS"
						:modelValue="filters.status || ''"
						@update:modelValue="(v) => setFilter('status', v)"
					/>
				</div>
			</div>
			<Button :tooltip="'Refresh'" icon="refresh-cw" :loading="loading" @click="reload()" />
		</div>

		<div class="flex min-h-0 flex-1">
			<!-- LEFT rail: run history on a standing gray-1 surface so the selected
			     row's white chip + shadow reads in light mode (§15.2 pattern) -->
			<div class="w-[360px] shrink-0 overflow-y-auto border-r bg-surface-gray-1">
				<template v-if="rows.length">
					<div class="flex flex-col divide-y">
						<button
							v-for="row in rows"
							:key="row.name"
							class="flex w-full items-start gap-3 px-4 py-3 text-left"
							:class="
								row.name === selectedId
									? 'bg-surface-selected shadow-sm'
									: 'hover:bg-surface-gray-2'
							"
							@click="selectRun(row)"
						>
							<div class="min-w-0 flex-1">
								<div class="flex min-w-0 items-center gap-1.5">
									<Tooltip :text="exactDate(row.started_at)">
										<span class="truncate text-base text-ink-gray-9">
											{{ timeAgo(row.started_at) || "Queued" }}
										</span>
									</Tooltip>
									<span class="shrink-0 text-sm text-ink-gray-5">
										· {{ row.trigger || "manual" }}
									</span>
								</div>
								<!-- a scribe writes wiki pages, not findings — show its pages
								     tally so a successful run never reads as "0 findings" -->
								<div
									v-if="row.nature === 'Scribe'"
									class="mt-1 truncate text-sm text-ink-gray-5"
								>
									{{ row.pages_written || 0 }} page{{
										(row.pages_written || 0) === 1 ? "" : "s"
									}}
									written
								</div>
								<div v-else class="mt-1 truncate text-sm text-ink-gray-5">
									{{ row.findings_count || 0 }} finding{{
										(row.findings_count || 0) === 1 ? "" : "s"
									}}
									<span v-if="row.blocker_count" class="text-ink-red-4">
										· {{ row.blocker_count }} blocker{{
											row.blocker_count === 1 ? "" : "s"
										}}
									</span>
								</div>
							</div>
							<!-- partial scans carry an extra indicator so truncated coverage
							     never blends in with clean completed runs -->
							<Tooltip
								v-if="row.status === 'partial'"
								text="Partial scan - coverage gaps"
							>
								<FeatherIcon
									name="alert-triangle"
									class="mt-1 size-3.5 shrink-0 text-ink-amber-3"
								/>
							</Tooltip>
							<!-- PP-4 preview/shadow: a shadow run is reviewer-only and NOT a
							     compliant attestation, so it must never read the same as a
							     live run. The lifecycle status badge (running/completed/…) is
							     an orthogonal axis; this violet pill carries the activation
							     axis independently. Badge has no violet theme in
							     frappe-ui 0.1.278, so this reuses the app's violet-pill
							     recipe (see triggers/TriggersListPane.vue). -->
							<span
								v-if="canReview && row.preparation_mode === 'shadow'"
								class="mt-0.5 inline-flex h-5 shrink-0 select-none items-center whitespace-nowrap rounded-full bg-surface-violet-1 px-1.5 text-xs text-ink-violet-1"
							>
								Preview
							</span>
							<Badge
								class="mt-0.5 shrink-0"
								variant="subtle"
								:theme="STATUS_THEME[row.status] || 'gray'"
								:label="row.status"
							/>
						</button>
					</div>
					<div class="flex items-center justify-between gap-2 border-t px-4 py-2">
						<Button
							v-if="hasMore"
							variant="ghost"
							label="Load More"
							:loading="loading"
							@click="loadMore()"
						/>
						<div v-else />
						<div class="text-sm text-ink-gray-5">{{ rows.length }} of {{ total }}</div>
					</div>
				</template>
				<div v-else-if="loading" class="flex h-full items-center justify-center">
					<JvSpinner />
				</div>
				<!-- persistent fetch-error state: a failed load must never read as "No runs" -->
				<div
					v-else-if="error"
					class="flex h-full items-center justify-center px-6 text-center text-sm text-ink-red-4"
				>
					{{ error }}
				</div>
				<div
					v-else
					class="flex h-full flex-col items-center justify-center gap-3 px-6 text-center"
				>
					<FeatherIcon name="activity" class="size-7.5 text-ink-gray-5" />
					<div class="flex flex-col items-center gap-1">
						<span class="text-lg font-medium text-ink-gray-8">{{
							emptyState.title
						}}</span>
						<span class="text-p-base text-ink-gray-6">{{
							emptyState.description
						}}</span>
					</div>
				</div>
			</div>

			<!-- RIGHT pane: the selected run's findings -->
			<div class="flex-1 overflow-y-auto">
				<!-- PP-4 shadow banner: the honest "Preview (shadow) - not a compliant
				     attestation" statement must live in the reviewer's primary screen,
				     not only in the detached fallback-dashboard HTML. Shown whenever the
				     selected run was prepared in shadow, above the findings pane. -->
				<div
					v-if="canReview && selectedRun && selectedRun.preparation_mode === 'shadow'"
					class="flex items-start gap-2 border-b bg-surface-violet-1 px-6 py-2.5 text-sm text-ink-violet-1"
				>
					<FeatherIcon name="eye" class="size-4 shrink-0" />
					<span>
						Preview (shadow) - this run is visible to the named reviewer only and is
						not a compliant attestation. Promote the capability to live before relying
						on its results.
					</span>
				</div>
				<div
					v-if="!selectedRun"
					class="flex h-full flex-col items-center justify-center gap-3 px-8 text-center"
				>
					<FeatherIcon name="clipboard" class="size-7.5 text-ink-gray-5" />
					<div class="flex flex-col items-center gap-1">
						<span class="text-lg font-medium text-ink-gray-8">Select a run</span>
						<span class="text-p-base text-ink-gray-6">
							Pick a run from the list to review its findings.
						</span>
					</div>
				</div>
				<FindingsPanel v-else :run="selectedRun" @stopped="refreshKeep" />
			</div>
		</div>
	</div>
</template>

<script setup>
// AgentRunsBoard - the Runs tab of /agents/:slug as a two-pane master-detail
// (Approval-Board §15.2 pattern; replaces the single-pane AgentRunsTab).
// LEFT: this owner's run history for ONE agent via useListPage →
// list_runs_page (search / status facet, Load More + "N of M"). RIGHT:
// FindingsPanel for the selected run. The parent's Run Now calls
// reload({selectNewest: true}) through the exposed handle so the freshly
// queued run is surfaced and selected even if a facet would hide it.
import { ref, computed, watch, onMounted, onBeforeUnmount } from "vue";
import { Badge, Button, FeatherIcon, FormControl, Tooltip } from "frappe-ui";
import FindingsPanel from "@/pages/agents/FindingsPanel.vue";
import JvSpinner from "@/components/JvSpinner.vue";
import { useListPage } from "@/composables/useListPage";
import { timeAgo, exactDate } from "@/utils/datetime";
import * as apiAgents from "@/api/agents";

const props = defineProps({
	agentName: { type: String, required: true }, // listing docname (list_runs_page filter)
	// jarvis#1062: shadow/attestation is REVIEWER vocabulary. The pill and the
	// banner below explain a distinction only a reviewer can act on (they hold the
	// promote control), so a plain user sees neither - not a softened version of
	// them, none. Defaults false so a caller that forgets the prop under-discloses
	// rather than leaking the reviewer surface.
	canReview: { type: Boolean, default: false },
});

// lifecycle status (did the run finish) is ONE axis; PP-4 preparation_mode
// ('shadow'|'live', snapshot of the installation's activation_state at launch)
// is an orthogonal axis rendered separately as the "Preview" pill/banner above.
// A shadow run must never read the same as a live one. preparation_mode is
// supplied per row by list_runs_page (agents_api.py) — see cross-file note.
const STATUS_THEME = {
	running: "blue",
	completed: "green",
	partial: "orange",
	failed: "red",
	stopped: "gray",
};
const STATUS_OPTIONS = [
	{ label: "All statuses", value: "" },
	{ label: "Running", value: "running" },
	{ label: "Completed", value: "completed" },
	{ label: "Partial", value: "partial" },
	{ label: "Failed", value: "failed" },
	{ label: "Stopped", value: "stopped" },
];

// ── rail data: useListPage + adapter onto listRunsPage's tab-less shape ──────
const {
	rows,
	total,
	hasMore,
	loading,
	error,
	search,
	filters,
	setFilter,
	setFilters,
	resetLoad,
	loadMore,
	refreshKeep,
} = useListPage({
	fetchFn: (p) =>
		apiAgents.listRunsPage({
			agent: props.agentName,
			status: (p.filters && p.filters.status) || "",
			search: p.search || "",
			sort: "recent",
			start: p.start,
			page_length: p.page_length,
		}),
	defaultSort: { field: "started_at", dir: "desc" },
	storageKey: "agent-runs",
});

const emptyState = computed(() => {
	if (search.value.trim() || filters.status) {
		return { title: "No matching runs", description: "Try a different status or search." };
	}
	return {
		title: "No runs yet",
		description: "Use Run Now or a schedule - every run lands here with its findings.",
	};
});

// ── #1062 C3: poll the rail every 10s while any VISIBLE run is running, so a
// running run's status/counters update without a manual refresh. This is the
// ONE refresh path for the runs list - it extends the visibilitychange
// machinery below (started/stopped from the same rows watcher, cleared on
// unmount and on tab-hidden) rather than adding a second interval alongside
// it. The tick itself re-checks visibility too (belt-and-braces against a
// missed visibilitychange event) and skips a request already in flight.
let pollTimer = null;
function startPoll() {
	if (pollTimer) return;
	pollTimer = setInterval(() => {
		if (!loading.value && document.visibilityState === "visible") refreshKeep();
	}, 10000);
}
function stopPoll() {
	if (pollTimer) {
		clearInterval(pollTimer);
		pollTimer = null;
	}
}

// ── selection (local - runs live under the agent's hash tab, no :id route) ──
const selectedRun = ref(null);
const selectedId = computed(() => (selectedRun.value && selectedRun.value.name) || "");

function selectRun(row) {
	selectedRun.value = row;
}

// auto-select the first row; on refresh, re-pin the selection to the fresh row
// object so a running run's status/counters flip live in the right pane. When
// the status/search facet excludes the selected run, fall over to the first
// row (or clear to the placeholder) - never leave a stale run that isn't in
// the rail.
watch(rows, (r) => {
	if (selectedRun.value) {
		const again = r.find((x) => x.name === selectedRun.value.name);
		selectedRun.value = again || r[0] || null;
	} else if (r.length) {
		selectRun(r[0]);
	}
	// start/stop the poll from the SAME rows change that already drives
	// selection - see the comment above startPoll/stopPoll.
	if (r.some((x) => x.status === "running") && document.visibilityState === "visible") {
		startPoll();
	} else {
		stopPoll();
	}
});

// slug switch without an unmount → hard reset (stale rows belong to the old agent)
watch(
	() => props.agentName,
	() => {
		selectedRun.value = null;
		resetLoad();
	}
);

// Run Now lands here: refresh and select the newest run. Facets that would
// hide a just-queued (running) run are cleared first so the jump always lands.
async function reload(opts = {}) {
	const selectNewest = !!(opts && opts.selectNewest);
	if (selectNewest && (filters.status || search.value.trim())) {
		search.value = "";
		await setFilters({});
	} else {
		await resetLoad();
	}
	if (selectNewest && rows.value.length) selectRun(rows.value[0]);
}
defineExpose({ reload });

// freshness: refetch the loaded window on tab-visible (running → completed),
// and resume the poll (#1062 C3) if a running run is still in the loaded
// window; a hidden tab stops it outright rather than let it fire unseen.
function onVisibility() {
	if (document.visibilityState === "visible") {
		refreshKeep();
		if (rows.value.some((x) => x.status === "running")) startPoll();
	} else {
		stopPoll();
	}
}
onMounted(() => document.addEventListener("visibilitychange", onVisibility));
onBeforeUnmount(() => {
	document.removeEventListener("visibilitychange", onVisibility);
	stopPoll();
});
</script>
