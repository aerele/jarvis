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
					<Badge
						variant="subtle"
						:theme="RUN_THEMES[row.status] || 'gray'"
						:label="statusLabel(row.status)"
					/>
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
	</div>
</template>

<script setup>
// RunsTab - the /macros/runs dashboard (DESIGN-V3 §5.9): stat cards from
// macro_run_stats, status/macro filters, runs table rendered with the stock
// ListView pieces (no selection), Stop / open-chat row actions, load-more
// footer, and live socket updates (macro:progress / macro:done → silent
// window refetch + stats reload).
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
	FeatherIcon,
	Tooltip,
	toast,
} from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import { timeAgo, exactDate } from "@/utils/datetime";
import * as api from "@/api";
import { errHtml } from "@/lib/errors";

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
const status = ref("");
const macro = ref("");
const pageLength = useStorage("jarvis-pl-macro-runs", 20);
const stats = ref(null);
const macrosList = ref([]); // macro filter dropdown (list_macros)

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

// ── data (monotonic request id - stale responses dropped, like useListPage) ──
let reqId = 0;

// mode: "reset" (page 1, replaces) | "more" (appends) | "keep" (silent window refetch)
async function fetchRuns(mode = "reset") {
	const id = ++reqId;
	const append = mode === "more";
	const limit =
		mode === "keep"
			? Math.min(Math.max(rows.value.length || pageLength.value, 1), 100)
			: pageLength.value;
	if (mode !== "keep") loading.value = true;
	try {
		const res =
			(await api.listMacroRuns({
				status: status.value,
				macro: macro.value,
				limit,
				start: append ? rows.value.length : 0,
			})) || {};
		if (id !== reqId) return;
		const nr = res.runs || [];
		rows.value = append ? [...rows.value, ...nr] : nr;
		hasMore.value = !!res.has_more;
		total.value = res.total != null ? res.total : null;
	} catch (e) {
		if (id !== reqId) return;
		toast.error(errHtml(e)); // keep last-good rows visible
	} finally {
		if (id === reqId && mode !== "keep") loading.value = false;
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
</script>
