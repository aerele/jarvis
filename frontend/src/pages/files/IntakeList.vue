<template>
	<ListPage
		:breadcrumbs="[{ label: 'File Box', route: { name: 'FilesList' } }]"
		:columns="columns"
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
		<template #right-header>
			<Select
				v-if="canManage"
				class="w-28"
				:options="SCOPE_OPTIONS"
				:model-value="scope"
				@update:model-value="(v) => (scope = v || 'mine')"
			/>
			<FileBoxSwitch :model-value="'intake'" @update:model-value="$emit('switch', $event)" />
		</template>

		<template #cell-file="{ row }">
			<div class="flex min-w-0 items-center gap-2 overflow-hidden">
				<FeatherIcon name="file-text" class="size-4 shrink-0 text-ink-gray-5" />
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
				<a
					v-if="intakeDraftLink(row) && intakeDraftLink(row).kind === 'desk'"
					:href="intakeDraftLink(row).href"
					target="_blank"
					rel="noopener noreferrer"
					class="text-ink-blue-3 hover:underline"
					>{{ intakeDraftLink(row).label }}</a
				>
				<router-link
					v-else-if="intakeDraftLink(row)"
					:to="intakeDraftLink(row).href"
					class="text-ink-blue-3 hover:underline"
					>{{ intakeDraftLink(row).label }}</router-link
				>
			</div>
		</template>

		<template #cell-summary="{ row }">
			<div class="truncate text-base text-ink-gray-7" :title="row.summary">
				{{ row.summary }}
			</div>
		</template>

		<template #cell-_retry="{ row }">
			<div class="flex w-full items-center justify-end">
				<Button
					v-if="row.can_retry"
					variant="ghost"
					size="sm"
					label="Retry"
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
import { ref, watch, onMounted, onBeforeUnmount } from "vue";
import { Badge, Button, FeatherIcon, Select, toast } from "frappe-ui";
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

const columns = [
	{ label: "File", key: "file", width: 3 },
	{ label: "Source", key: "source", width: 2 },
	{ label: "Status", key: "status", width: "9rem" },
	{ label: "Draft", key: "draft", width: 2 },
	{ label: "Summary", key: "summary", width: 3 },
	{ label: "", key: "_retry", width: "5rem", align: "right" },
];
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
async function retry(row) {
	if (retrying.value) return;
	retrying.value = row.name;
	try {
		await intakeRetry(row.name);
		toast.success("Queued again");
		refreshKeep();
	} catch (e) {
		toast.error(errHtml(e));
		refreshKeep();
	} finally {
		retrying.value = "";
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
