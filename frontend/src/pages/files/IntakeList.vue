<template>
	<ListPage
		:breadcrumbs="[{ label: 'File Box', route: { name: 'FilesList' } }]"
		:columns="columns"
		:row-height="narrow ? 'auto' : 40"
		:rows="rows"
		:loading="loading"
		:error="error"
		:total="total"
		:has-more="hasMore"
		:quick-filters="quickFilters"
		:filter-defs="filterDefs"
		:filters="filters"
		:sort-options="[]"
		:sort="sort"
		:page-length="pageLength"
		:default-sort="DEFAULT_SORT"
		storage-key="files-intake"
		:empty-state="{
			icon: 'inbox',
			title: 'No intake files yet',
			description: 'Files dropped into a watched folder show up here.',
		}"
		@update:filters="setFilters"
		@update:page-length="(v) => (pageLength = v)"
		@load-more="loadMore"
		@refresh="resetLoad"
	>
		<template #banner>
			<div ref="statusEl" tabindex="-1" role="status" class="sr-only">{{ statusText }}</div>
		</template>

		<template #right-header>
			<!-- wraps instead of clipping: the switch must stay reachable at 320 px -->
			<div class="flex min-w-0 flex-wrap items-center justify-end gap-2">
				<Select
					v-if="canManage"
					class="w-28"
					aria-label="Whose files"
					:options="SCOPE_OPTIONS"
					:model-value="scope"
					@update:model-value="(v) => (scope = v || 'mine')"
				/>
				<FileBoxSwitch
					:model-value="'intake'"
					@update:model-value="$emit('switch', $event)"
				/>
			</div>
		</template>

		<template #cell-file="{ row }">
			<!-- Narrow screens: the one visible column is a card with everything the other
			     columns would show, so nothing is squeezed and nothing scrolls sideways. -->
			<div v-if="narrow" class="flex min-w-0 flex-col gap-1 py-2" data-testid="intake-card">
				<div class="flex min-w-0 items-center gap-2">
					<FeatherIcon name="file-text" class="size-4 shrink-0 text-ink-gray-7" />
					<div class="min-w-0 break-words font-medium text-ink-gray-9">
						{{ row.file_name }}
					</div>
				</div>
				<div class="flex flex-wrap items-center gap-x-2 gap-y-1">
					<Badge
						variant="subtle"
						:theme="intakeBadge(row).theme"
						:label="intakeBadge(row).label"
					/>
					<IntakeDraftLink :row="row" />
				</div>
				<div class="break-words text-sm text-ink-gray-7">{{ row.summary }}</div>
				<div class="break-words text-sm text-ink-gray-7">{{ row.source_label }}</div>
			</div>
			<div v-else class="flex min-w-0 items-center gap-2 overflow-hidden">
				<FeatherIcon name="file-text" class="size-4 shrink-0 text-ink-gray-7" />
				<div class="truncate text-base font-medium text-ink-gray-9" :title="row.file_name">
					{{ row.file_name }}
				</div>
			</div>
		</template>

		<template #cell-source="{ row }">
			<div class="truncate text-base text-ink-gray-7" :title="row.source_label">
				{{ row.source_label }}
			</div>
		</template>

		<template #cell-status="{ row }">
			<Badge
				variant="subtle"
				:theme="intakeBadge(row).theme"
				:label="intakeBadge(row).label"
			/>
		</template>

		<template #cell-draft="{ row }">
			<div class="min-w-0 truncate text-base">
				<IntakeDraftLink :row="row" />
			</div>
		</template>

		<template #cell-summary="{ row }">
			<div class="truncate text-base text-ink-gray-7" :title="row.summary">
				{{ row.summary }}
			</div>
		</template>

		<template #cell-_retry="{ row }">
			<div class="flex w-full items-center justify-end">
				<!-- the file name is in the name, so a screen reader can tell the Retry buttons apart -->
				<Button
					v-if="row.can_retry"
					variant="ghost"
					size="sm"
					label="Retry"
					:aria-label="`Retry ${row.file_name || row.name}`"
					:loading="retrying === row.name"
					:disabled="!!retrying"
					@click="retry(row)"
				/>
			</div>
		</template>
	</ListPage>
</template>

<script setup>
// File Box "Intake" view: the Document Intake files the caller uploaded (or, for
// managers, everyone's), with their status, draft and a Retry for failed ones.
// Polls every 10 s while a row is Queued or Running.
import { ref, computed, nextTick, watch, onMounted, onBeforeUnmount } from "vue";
import { Badge, Button, FeatherIcon, Select, toast } from "frappe-ui";
import IntakeDraftLink from "./IntakeDraftLink.vue";
import { useNarrow } from "@/composables/useNarrow";
import ListPage from "@/components/list/ListPage.vue";
import FileBoxSwitch from "./FileBoxSwitch.vue";
import { useListPage } from "@/composables/useListPage";
import { intakeListPage, intakeRetry } from "@/api/intake";
import { getTriggersCaps } from "@/api/triggers";
import { errHtml } from "@/lib/errors";
import {
	INTAKE_STATUS_OPTIONS,
	intakeBadge,
	intakeDraftLink,
	intakeIsLive,
} from "@/lib/intakeStatus";

defineEmits(["switch"]);

const ALL_COLUMNS = [
	{ label: "File", key: "file", width: 3 },
	{ label: "Source", key: "source", width: 2 },
	{ label: "Status", key: "status", width: "9rem" },
	{ label: "Draft", key: "draft", width: 2 },
	{ label: "Summary", key: "summary", width: 3 },
	{ label: "", key: "_retry", width: "5rem", align: "right" },
];
// Below ~640 px six columns do not fit: the File cell turns into a card that carries the
// status, draft, summary and source, and only Retry stays beside it (same lever as
// SupportListPage and ActivityTab: a narrower `columns` prop).
const narrow = useNarrow();
const columns = computed(() =>
	narrow.value ? ALL_COLUMNS.filter((c) => c.key === "file" || c.key === "_retry") : ALL_COLUMNS
);
const quickFilters = [
	{ key: "search", label: "Search files", type: "text" },
	{ key: "status", label: "Status", type: "select", options: INTAKE_STATUS_OPTIONS },
];
const filterDefs = [
	{ key: "status", label: "Status", type: "select", options: INTAKE_STATUS_OPTIONS },
	{ key: "daterange", label: "Added", type: "daterange" },
];
const DEFAULT_SORT = { field: "creation", dir: "desc" };
const SCOPE_OPTIONS = [
	{ label: "Mine", value: "mine" },
	{ label: "All", value: "all" },
];

const canManage = ref(false);
const scope = ref("mine");

const {
	rows,
	total,
	hasMore,
	loading,
	error,
	filters,
	setFilters,
	sort,
	pageLength,
	resetLoad,
	loadMore,
	refreshKeep,
} = useListPage({
	fetchFn: (p) => {
		// the backend whitelists filter keys: search rides its own param
		const { search: q, ...rest } = p.filters || {};
		return intakeListPage({
			...p,
			search: q || p.search || "",
			filters: rest,
			scope: scope.value,
		});
	},
	defaultSort: DEFAULT_SORT,
	storageKey: "files-intake",
});

watch(scope, () => resetLoad());

const retrying = ref("");
// A busy frappe-ui Button is disabled, and a disabled control loses focus, so after a
// retry focus goes to a stable status line (also read out by screen readers).
const statusEl = ref(null);
const statusText = ref("");
async function retry(row) {
	if (retrying.value) return;
	retrying.value = row.name;
	try {
		await intakeRetry(row.name);
		toast.success("Queued again");
		statusText.value = `${row.file_name || row.name} queued again`;
	} catch (e) {
		toast.error(errHtml(e));
		statusText.value = `${row.file_name || row.name} could not be queued again`;
	} finally {
		retrying.value = "";
		refreshKeep();
		await nextTick();
		if (statusEl.value) statusEl.value.focus();
	}
}

let pollTimer = null;
function stopPolling() {
	if (pollTimer) clearInterval(pollTimer);
	pollTimer = null;
}
// A hidden tab does not poll; coming back refreshes once and resumes.
function tick() {
	if (!document.hidden) refreshKeep();
}
function onVisibility() {
	if (!document.hidden && pollTimer) refreshKeep();
}
// Poll only while a visible row is still moving; stop when none is.
watch(
	rows,
	(list) => {
		if ((list || []).some(intakeIsLive)) {
			if (!pollTimer) pollTimer = setInterval(tick, 10000);
		} else {
			stopPolling();
		}
	},
	{ immediate: true }
);
onMounted(async () => {
	document.addEventListener("visibilitychange", onVisibility);
	try {
		canManage.value = !!(await getTriggersCaps())?.can_manage;
	} catch (e) {
		canManage.value = false;
	}
});
onBeforeUnmount(() => {
	stopPolling();
	document.removeEventListener("visibilitychange", onVisibility);
});
</script>
