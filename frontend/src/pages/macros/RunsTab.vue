<template>
	<div class="flex h-full flex-col overflow-hidden">
		<LayoutHeader>
			<template #left-header>
				<Breadcrumbs :items="[{ label: 'Macros', route: { name: 'MacrosList' } }]" />
			</template>
		</LayoutHeader>

		<!-- stat cards (§5.9 Runs tab) -->
		<div class="grid shrink-0 grid-cols-2 gap-4 px-5 pt-4 lg:grid-cols-4">
			<div v-for="card in statCards" :key="card.label" class="rounded-lg border p-4">
				<div class="text-sm text-ink-gray-5">{{ card.label }}</div>
				<div class="mt-1 text-2xl font-semibold text-ink-gray-9">{{ card.value }}</div>
			</div>
		</div>

		<!-- controls: status + macro selects | refresh -->
		<div class="flex shrink-0 items-center justify-between gap-2 px-5 py-4">
			<div class="-ml-1 flex h-9 flex-1 items-center overflow-x-auto">
				<div class="m-1 min-w-36">
					<FormControl
						type="select"
						:options="STATUS_OPTIONS"
						:modelValue="status"
						@update:modelValue="setStatus"
					/>
				</div>
				<div class="m-1 min-w-36">
					<FormControl
						type="select"
						:options="macroOptions"
						:modelValue="macro"
						@update:modelValue="setMacro"
					/>
				</div>
			</div>
			<div class="-ml-2 h-[70%] border-l" />
			<Button :tooltip="'Refresh'" icon="refresh-cw" :loading="loading" @click="reload" />
		</div>

		<!-- runs list (same ListView pieces, no selection) -->
		<ListView
			v-if="rows.length"
			class="min-h-0 flex-1"
			:columns="columns"
			:rows="rows"
			row-key="name"
			:options="{
				selectable: false,
				onRowClick: openRow,
				rowHeight: 40,
				resizeColumn: false,
				showTooltip: true,
			}"
		>
			<template #default>
				<ListHeader class="sm:mx-5 mx-3">
					<ListHeaderItem v-for="column in columns" :key="column.key" :item="column" />
				</ListHeader>
				<ListRows class="mx-3 sm:mx-5" />
			</template>
			<template #cell="{ column, row, item, align }">
				<template v-if="column.key === 'status'">
					<div class="flex items-center gap-1.5">
						<Badge
							variant="subtle"
							:theme="RUN_THEMES[row.status] || 'gray'"
							:label="statusLabel(row.status)"
						/>
						<!-- A run with something to say explains itself: hover = the first
						     line, click = dialog with the full text + finish time + run
						     mode. Keyed on `error` PRESENCE, not status: a failed run's
						     reason, why a run stopped, or a completed run's note; a
						     user-cancelled `stopped` run (empty) shows no affordance.
						     @click.stop so it never bubbles into openRow's navigation. -->
						<Tooltip v-if="row.error" :text="shortError(row.error)">
							<button
								type="button"
								class="flex text-ink-gray-5 hover:text-ink-gray-7"
								:aria-label="runDetail(row).title + ': details'"
								@click.stop.prevent="openError(row)"
							>
								<FeatherIcon name="info" class="size-3.5" />
							</button>
						</Tooltip>
					</div>
				</template>
				<template v-else-if="column.key === 'started_at'">
					<Tooltip :text="exactDate(row.started_at || row.creation)">
						<div class="truncate text-base">
							{{ timeAgo(row.started_at || row.creation) }}
						</div>
					</Tooltip>
				</template>
				<template v-else-if="column.key === 'duration'">
					<div class="truncate text-base">
						{{ row.duration_s != null ? fmtDuration(row.duration_s) : "-" }}
					</div>
				</template>
				<template v-else-if="column.key === 'steps'">
					<div class="truncate text-base">
						{{ (row.current_step || 0) + "/" + (row.total_steps || 0) }}
					</div>
				</template>
				<template v-else-if="column.key === '_actions'">
					<div class="flex w-full items-center justify-end gap-1" @click.stop.prevent>
						<Button
							v-if="
								row.status === 'running' ||
								row.status === 'queued' ||
								row.status === 'waiting_capacity'
							"
							variant="ghost"
							theme="red"
							icon="square"
							:tooltip="'Stop'"
							@click="stopRun(row)"
						/>
						<Button
							v-if="row.conversation"
							variant="ghost"
							icon="message-circle"
							:tooltip="'Open chat'"
							@click="router.push('/c/' + row.conversation)"
						/>
					</div>
				</template>
				<ListRowItem v-else :column="column" :row="row" :item="item" :align="align" />
			</template>
		</ListView>

		<!-- error state (fetch failed, no rows to show) - before the empty state, so
		     a load that failed is not reported as "No macro runs yet". Same markup
		     as ListPage's own error state, "Try again" included. -->
		<div v-else-if="error" class="relative flex-1">
			<div
				class="absolute left-1/2 flex w-4/12 -translate-x-1/2 flex-col items-center gap-3"
				:style="{ top: '35%' }"
			>
				<FeatherIcon name="alert-circle" class="size-7.5 text-ink-red-4" />
				<div class="flex flex-col items-center gap-1">
					<span class="text-lg font-medium text-ink-gray-8"
						>Couldn't load macro runs</span
					>
					<span class="text-center text-p-base text-ink-red-4">{{ error }}</span>
				</div>
				<Button label="Try again" :loading="loading" @click="reload" />
			</div>
		</div>

		<!-- empty state (loaded, zero rows) -->
		<div v-else-if="!loading" class="relative flex-1">
			<div
				class="absolute left-1/2 flex w-4/12 -translate-x-1/2 flex-col items-center gap-3"
				:style="{ top: '35%' }"
			>
				<FeatherIcon name="activity" class="size-7.5 text-ink-gray-5" />
				<div class="flex flex-col items-center gap-1">
					<span class="text-lg font-medium text-ink-gray-8">No macro runs yet</span>
					<span class="text-center text-p-base text-ink-gray-6">
						Run a macro to see its history here.
					</span>
				</div>
			</div>
		</div>
		<div v-else class="flex-1" />

		<ListFooter
			v-if="rows.length"
			class="border-t sm:px-5 px-3 py-2"
			:modelValue="pageLength"
			:options="{ rowCount: rows.length, totalCount: totalCount }"
			@update:modelValue="(v) => (pageLength = v)"
			@loadMore="loadMore"
		/>

		<!-- failure detail (purpose-built, NOT the shared triggers dialog): a run's
		     error can be a full traceback, so it earns a dialog over a tooltip alone. -->
		<Dialog v-model="errDialog.show" :options="{ title: errDialogTitle, size: 'lg' }">
			<template #body-content>
				<div v-if="errDialog.row" class="flex flex-col gap-3">
					<div class="flex flex-wrap gap-x-6 gap-y-1 text-sm text-ink-gray-6">
						<span v-if="errDialog.row.macro_name">{{ errDialog.row.macro_name }}</span>
						<span v-if="errDialog.row.finished_at">
							Finished {{ timeAgo(errDialog.row.finished_at) }}
						</span>
						<span v-if="runModeLabel(errDialog.row.run_mode)">
							{{ runModeLabel(errDialog.row.run_mode) }} run
						</span>
					</div>
					<div>
						<div class="mb-1 text-sm font-medium text-ink-gray-7">
							{{ errDialogDetail.label }}
						</div>
						<pre
							class="max-h-80 overflow-auto whitespace-pre-wrap rounded-md bg-surface-gray-2 p-3 text-xs text-ink-gray-8"
							>{{ errDialog.row.error }}</pre
						>
					</div>
				</div>
			</template>
		</Dialog>
	</div>
</template>

<script setup>
// RunsTab - the /macros/runs dashboard (DESIGN-V3 §5.9): stat cards from
// macro_run_stats, status/macro filters, runs table rendered with the stock
// ListView pieces (no selection), Stop / open-chat row actions, load-more
// footer, and live socket updates (macro:progress / macro:done → silent
// refetch of every loaded page + stats reload).
import { ref, computed, watch, inject, onMounted, onBeforeUnmount } from "vue";
import { useRouter } from "vue-router";
import { useStorage } from "@vueuse/core";
import {
	ListView,
	ListHeader,
	ListHeaderItem,
	ListRows,
	ListRowItem,
	ListFooter,
	Breadcrumbs,
	Button,
	Badge,
	FormControl,
	Dialog,
	FeatherIcon,
	Tooltip,
	toast,
} from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import { timeAgo, exactDate } from "@/utils/datetime";
import * as api from "@/api";
import { errMessage as errMsg, errHtml } from "@/lib/errors";
import { runDetail } from "@/lib/macroRunOutcome";

const router = useRouter();
const socket = inject("$socket");

// ── list config ──────────────────────────────────────────────────────────────
const STATUS_OPTIONS = [
	{ label: "All statuses", value: "" },
	{ label: "Queued", value: "queued" },
	{ label: "Running", value: "running" },
	{ label: "Waiting for capacity", value: "waiting_capacity" },
	{ label: "Completed", value: "completed" },
	{ label: "Failed", value: "failed" },
	{ label: "Stopped", value: "stopped" },
];
const RUN_THEMES = {
	queued: "gray",
	running: "blue",
	waiting_capacity: "orange", // parked, not failed - matches the merge_status "pending" badge elsewhere in Macros
	completed: "green",
	failed: "red",
	stopped: "gray",
};
// Statuses whose display label isn't just a capitalized value (e.g. waiting_capacity).
const STATUS_LABELS = {
	waiting_capacity: "Waiting for capacity",
};

const columns = [
	{ label: "Macro", key: "macro_name", width: 2 },
	{ label: "Status", key: "status", width: "7rem" },
	{ label: "Trigger", key: "trigger", width: "6rem" },
	{ label: "Started", key: "started_at", width: "8rem" },
	{ label: "Duration", key: "duration", width: "6rem" },
	{ label: "Progress", key: "steps", width: "6rem" },
	{ label: "", key: "_actions", width: "8rem", align: "right" },
];

// ── state ────────────────────────────────────────────────────────────────────
const rows = ref([]);
const hasMore = ref(false);
const total = ref(null); // §8.3/D38: list_macro_runs gains `total`
const loading = ref(false);
// Why the last fetch failed. Shown in place of the empty state only: with rows on
// screen the last-good rows stay and the toast says it. Cleared when any fetch
// works, and when a load starts under filters other than the ones that failed.
const error = ref("");
const status = ref("");
const macro = ref("");
const pageLength = useStorage("jarvis-pl-macro-runs", 20);
const stats = ref(null);
const macrosList = ref([]); // macro filter dropdown (list_macros)
// failure-detail dialog (RunsTab-local; a run's error can be a full traceback)
const errDialog = ref({ show: false, row: null });
// Titled for what the run did: the stored text is the reason a run failed or
// stopped, and a plain note on a completed one (lib/macroRunOutcome.js).
const errDialogDetail = computed(() => runDetail(errDialog.value.row));
const errDialogTitle = computed(() => errDialogDetail.value.title);

const macroOptions = computed(() => [
	{ label: "All macros", value: "" },
	...macrosList.value.map((m) => ({ label: m.macro_name || m.name, value: m.name })),
]);

// footer "N of M"; until the backend `total` lands, keep Load More alive via
// a rows+1 fallback while has_more says there is more
const totalCount = computed(() => {
	if (total.value != null) return total.value;
	return hasMore.value ? rows.value.length + 1 : rows.value.length;
});

const statCards = computed(() => {
	const s = stats.value || {};
	return [
		{ label: "Total runs", value: s.total != null ? s.total : "-" },
		{ label: "Success rate", value: s.success_rate != null ? `${s.success_rate}%` : "-" },
		{ label: "Running now", value: s.running != null ? s.running : "-" },
		{ label: "Last run", value: s.last_run_at ? timeAgo(s.last_run_at) : "-" },
	];
});

// ── data ─────────────────────────────────────────────────────────────────────
// Three kinds of fetch:
//   "reset"  page 1, replaces the list (first load, Refresh, a filter or page
//            length change, Try again)
//   "more"   the next page, appended (Load More)
//   "keep"   a silent refresh after a realtime event: every page already on
//            screen is fetched again and the list is replaced with the result
//
// One fetch is in flight at a time, and the rules are:
//   - "reset" and "more" are things the user asked for. They start at once, and
//     the newest wins (a monotonic request id drops the answer of an older one).
//   - "keep" NEVER supersedes one of those. If anything is in flight it waits,
//     and runs once when the tab goes idle, however many events arrived.
//   - a "keep" in flight gives way to a "reset" (which brings fresh rows anyway)
//     or a "more" (after which it runs again).
//   - a "keep" whose request has not answered in KEEP_LOST_MS gives way to the
//     next "keep". Requests have no timeout, so a lost one would otherwise hold
//     every later refresh back with nothing on screen to say so.
// So the rows on screen were always fetched under the filters now selected, and
// are only ever joined with rows fetched under those same filters. When a keep
// could overtake a filter change, it spliced the new filter's first page over
// the old filter's rows: wrong rows, a dead Load More, a dropped Load More.
let reqId = 0;
let inFlight = ""; // the kind of fetch under way, "" when idle
let keepQueued = false;
let disposed = false;
// The filters the rows on screen were loaded under.
let loadedFilters = "";
// The filters the error on screen is about.
let failedFilters = "";
// When the keep in flight last sent a request.
let keepSentAt = 0;
const KEEP_LOST_MS = 30000;
// A failing silent refresh is said once, not once per realtime event for as long
// as the server is down. Any fetch that works re-arms it.
let failureSaid = false;

// The server returns at most 100 rows a page.
const PAGE_MAX = 100;
// A silent refresh re-reads this many pages at most. Past that the list falls
// back to its first 500 rows, and Load More continues from there.
const KEEP_MAX_PAGES = 5;

// Every row on screen again, in pages of PAGE_MAX, one request after the other,
// all under `filters`. Returns null when a newer fetch took over part-way. A
// short list asks for a full page, so a new run can appear in it.
async function fetchLoadedPages(filters, id) {
	const want = Math.min(
		Math.max(rows.value.length, pageLength.value, 1),
		PAGE_MAX * KEEP_MAX_PAGES
	);
	const out = [];
	const seen = new Set();
	let res = {};
	let start = 0;
	while (start < want) {
		const limit = Math.min(PAGE_MAX, want - start);
		keepSentAt = Date.now();
		res = (await api.listMacroRuns({ ...filters, limit, start })) || {};
		if (id !== reqId) return null;
		// A run that started between two requests pushes a row from the end of
		// one page to the top of the next: show it once.
		for (const r of res.runs || []) {
			if (!seen.has(r.name)) {
				seen.add(r.name);
				out.push(r);
			}
		}
		start += limit;
		if (!res.has_more) break;
	}
	return { runs: out, has_more: !!res.has_more, total: res.total };
}

async function fetchRuns(mode = "reset") {
	if (disposed) return;
	if (mode === "keep" && inFlight) {
		const lost = inFlight === "keep" && Date.now() - keepSentAt > KEEP_LOST_MS;
		if (!lost) {
			keepQueued = true;
			return;
		}
	}
	// A keep this "more" is about to supersede runs again afterwards. A keep that
	// starts is the one any earlier event was waiting for.
	if (mode === "more" && inFlight === "keep") keepQueued = true;
	if (mode === "keep") keepQueued = false;
	const id = ++reqId;
	inFlight = mode;
	const filters = { status: status.value, macro: macro.value };
	const filtersKey = JSON.stringify(filters);
	if (mode !== "keep") {
		loading.value = true;
		// A retry under the filters that failed keeps the error up while it loads,
		// so "Try again" shows its spinner (as ListPage does) and the pane does not
		// blink blank. An error about other filters goes at once. A silent refresh
		// leaves it up until it works: there is no loading state to show in its
		// place, and "No macro runs yet" would flash instead.
		if (filtersKey !== failedFilters) error.value = "";
	}
	try {
		const res =
			mode === "keep"
				? await fetchLoadedPages(filters, id)
				: (await api.listMacroRuns({
						...filters,
						limit: pageLength.value,
						start: mode === "more" ? rows.value.length : 0,
				  })) || {};
		if (id !== reqId || !res) return;
		const nr = res.runs || [];
		rows.value = mode === "more" ? [...rows.value, ...nr] : nr;
		hasMore.value = !!res.has_more;
		total.value = res.total != null ? res.total : null;
		loadedFilters = filtersKey;
		error.value = "";
		failureSaid = false;
	} catch (e) {
		if (id !== reqId) return;
		error.value = errMsg(e);
		failedFilters = filtersKey;
		// A refresh that fails keeps the last-good rows. A FILTER CHANGE that fails
		// does not: the rows on screen belong to the filter that was left.
		if (filtersKey !== loadedFilters) {
			rows.value = [];
			hasMore.value = false;
			total.value = null;
		}
		if (mode !== "keep" || !failureSaid) toast.error(errHtml(e));
		failureSaid = true;
	} finally {
		// The NEWEST fetch clears the spinner whatever its kind, and hands over to
		// a refresh that was waiting.
		if (id === reqId) {
			inFlight = "";
			loading.value = false;
			if (keepQueued) {
				keepQueued = false;
				fetchRuns("keep");
			}
		}
	}
}

async function loadStats() {
	try {
		stats.value = await api.macroRunStats();
	} catch (e) {
		// keep prior
	}
}

async function loadMacros() {
	try {
		macrosList.value = (await api.listMacros()) || [];
	} catch (e) {
		// keep prior
	}
}

function reload() {
	fetchRuns("reset");
	loadStats();
}

function loadMore() {
	if (!hasMore.value || loading.value) return;
	fetchRuns("more");
}

function setStatus(v) {
	status.value = v || "";
	fetchRuns("reset");
}

function setMacro(v) {
	macro.value = v || "";
	fetchRuns("reset");
}

watch(pageLength, () => fetchRuns("reset"));

// ── row actions ──────────────────────────────────────────────────────────────
function openRow(row) {
	if (row.conversation) router.push("/c/" + row.conversation);
}

function openError(row) {
	errDialog.value = { show: true, row };
}

async function stopRun(row) {
	try {
		await api.stopMacroRun(row.name);
		row.status = "stopped"; // optimistic patch
		toast.success("Run stopped");
		fetchRuns("keep");
		loadStats();
	} catch (e) {
		toast.error(errHtml(e));
	}
}

// ── live updates (§5.9): macro:progress / macro:done → refreshKeep + stats ───
function onEvent(p) {
	if (!p || !p.kind) return;
	if (p.kind === "macro:progress" || p.kind === "macro:done") {
		fetchRuns("keep");
		loadStats();
	}
}

onMounted(() => {
	fetchRuns("reset");
	loadStats();
	loadMacros();
	socket && socket.on && socket.on("jarvis:event", onEvent);
});
onBeforeUnmount(() => {
	disposed = true; // a refresh still waiting has nothing to refresh
	// Whatever is in flight is now nobody's: a keep stops paging, and a failure
	// is not toasted over the page the user went to.
	reqId++;
	socket && socket.off && socket.off("jarvis:event", onEvent);
});

// ── formatters ───────────────────────────────────────────────────────────────
function statusLabel(s) {
	if (!s) return "";
	return STATUS_LABELS[s] || s.charAt(0).toUpperCase() + s.slice(1);
}
function fmtDuration(sec) {
	if (sec == null) return "";
	sec = Math.max(0, Math.round(sec));
	if (sec < 60) return `${sec}s`;
	const m = Math.floor(sec / 60);
	const s = sec % 60;
	if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
	const h = Math.floor(m / 60);
	return `${h}h ${m % 60}m`;
}
// run_mode is a stored Select with no default: NULL on legacy/parked runs, so
// render nothing rather than "null".
function runModeLabel(m) {
	if (m === "merged") return "Merged";
	if (m === "stepped") return "Stepped";
	return "";
}
// First non-blank line of the error, clipped - the tooltip is the teaser; the
// dialog carries the full text.
function shortError(e) {
	if (!e) return "";
	const first =
		String(e)
			.split("\n")
			.find((l) => l.trim()) || "";
	return first.length > 140 ? first.slice(0, 139) + "…" : first;
}
</script>
