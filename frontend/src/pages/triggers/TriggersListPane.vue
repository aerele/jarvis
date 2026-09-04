<template>
	<ListPage
		:breadcrumbs="[{ label: 'Triggers', route: { name: 'TriggersPage' } }]"
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
		:selectable="!!caps.can_manage"
		:get-row-route="getRowRoute"
		storage-key="triggers"
		:empty-state="{
			icon: 'git-branch',
			title: 'No triggers yet',
			description:
				'Ask in the chat on the left, e.g. \'Warn me when a Sales Invoice over 1 lakh is submitted\'',
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
				v-if="caps.can_manage"
				variant="solid"
				label="New trigger"
				iconLeft="plus"
				@click="router.push({ name: 'TriggerNew' })"
			/>
		</template>

		<!-- enabled: inline Switch (admins, optimistic) / read-only badge -->
		<template #cell-enabled="{ row }">
			<div v-if="caps.can_manage" class="flex w-full items-center" @click.stop.prevent>
				<Switch
					:modelValue="!!row.enabled"
					:disabled="togglingRow === row.name"
					@update:modelValue="(v) => toggleEnabled(row, v)"
				/>
			</div>
			<Badge
				v-else
				variant="subtle"
				:theme="row.enabled ? 'green' : 'gray'"
				:label="row.enabled ? 'On' : 'Off'"
			/>
		</template>

		<!-- custom #cell slots bypass ListRowItem's Tooltip wrapper, so each
		     truncating cell carries a native title to keep the full value
		     recoverable on hover -->
		<template #cell-trigger_name="{ row }">
			<div
				class="truncate text-base font-medium text-ink-gray-9"
				:title="row.trigger_name || row.name"
			>
				{{ row.trigger_name || row.name }}
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
			<!-- LLM: violet pill - Badge has no purple theme in frappe-ui 0.1.278,
			     so this composes the same subtle-badge recipe from the emitted
			     violet semantic tokens (Badge md metrics: h-5 px-1.5 text-xs) -->
			<span
				v-else-if="row.action_type === 'LLM'"
				class="inline-flex h-5 select-none items-center whitespace-nowrap rounded-full bg-surface-violet-1 px-1.5 text-xs text-ink-violet-1"
			>
				LLM
			</span>
			<span v-else class="text-base text-ink-gray-4">-</span>
		</template>

		<template #cell-activity_24h="{ row }">
			<div class="flex w-full items-center justify-center text-base text-ink-gray-5">
				{{ row.activity_24h || 0 }}
			</div>
		</template>

		<template #cell-last_activity_at="{ row }">
			<Tooltip v-if="row.last_activity_at" :text="exactDate(row.last_activity_at)">
				<div class="truncate text-base">{{ timeAgo(row.last_activity_at) }}</div>
			</Tooltip>
			<span v-else class="text-base text-ink-gray-4">-</span>
		</template>

		<template #select-actions="{ selections, unselectAll }">
			<Dropdown
				v-if="caps.can_manage"
				:options="[
					{ label: 'Delete', onClick: () => bulkDelete(selections, unselectAll) },
				]"
			>
				<Button icon="more-horizontal" variant="ghost" />
			</Dropdown>
		</template>
	</ListPage>
</template>

<script setup>
// TriggersListPane - the right pane of the Triggers tab: standard ListPage +
// useListPage against triggers_api.list_triggers_page (search · quick filters ·
// Filter/Sort/Columns · Load-More pagination, the MacrosList shape). Admins get
// the inline enabled Switch (optimistic set_trigger_enabled), New trigger and
// bulk Delete (server cap 50); everyone else reads. The parent refreshes this
// pane via the exposed refresh() when the chat pane's run ends or a
// trigger:changed frame arrives.
import { ref, computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Badge, Button, Dropdown, Switch, Tooltip, toast, confirmDialog } from "frappe-ui";
import ListPage from "@/components/list/ListPage.vue";
import { useListPage } from "@/composables/useListPage";
import { triggersListFetch } from "@/pages/list/listFetchers";
import { timeAgo, exactDate } from "@/utils/datetime";
import * as apiTriggers from "@/api/triggers";
import { errHtml } from "@/lib/errors";

const props = defineProps({
	caps: { type: Object, default: () => ({}) }, // get_triggers_caps payload
});

const router = useRouter();
// /triggers is a TABBED route (Triggers | Activity); the payload carries its view
// key so the Activity tab never applies this list's filters (judgment C08-7).
const route = useRoute();

// ── list config ──────────────────────────────────────────────────────────────
const ENABLED_OPTIONS = [
	{ label: "All", value: "" },
	{ label: "Enabled", value: "1" },
	{ label: "Disabled", value: "0" },
];
const ACTION_OPTIONS = [
	{ label: "All actions", value: "" },
	{ label: "Script", value: "Script" },
	{ label: "LLM", value: "LLM" },
];

// Column budget: the list shares the tab with a 380px chat pane, so at a
// 1440px viewport it only gets ~800px — fixed tracks are kept lean so the
// minmax(0, Nfr) Name/DocType tracks keep real reading room. Event gets
// 10.5rem: the longest label ("Before Save (blockable)") must show its
// qualifier, not clip at "(block…". "Updated" is deliberately not a default
// column (modified stays the default sort; last_activity_at is the
// operationally useful timestamp here).
const columns = [
	{ label: "On", key: "enabled", width: "3.5rem" },
	{ label: "Name", key: "trigger_name", width: 2 },
	{ label: "DocType", key: "target_doctype", width: 1 },
	{ label: "Event", key: "doc_event", width: "10.5rem" },
	{ label: "Action", key: "action_type", width: "4.5rem" },
	{ label: "24h", key: "activity_24h", width: "3rem", align: "center" },
	{ label: "Last activity", key: "last_activity_at", width: "7.5rem" },
];

// search rides the quick-filter strip (MacrosList/SkillsList pattern): it lives
// in the filters object for a controlled input; fetchFn moves it onto the
// envelope's `search` param. No separate DocType input: server-side search
// already matches target_doctype, and the strip shares an ~800px pane with
// the chat rail (each quick filter reserves min-w-36).
const quickFilters = computed(() => [
	{ key: "search", label: "Search triggers", type: "text" },
	{ key: "enabled", label: "Status", type: "select", options: ENABLED_OPTIONS },
	{ key: "action_type", label: "Action", type: "select", options: ACTION_OPTIONS },
]);
// plan 08 wave 1: the curated `filterDefs` array is GONE as the filter catalog.
// The panel is generated from the server schema for view_key "triggers". The
// trigger's LOGIC fields (condition / script_body / llm_instruction) are
// withheld by the registry's `excluded_fields`, because a `like` filter over
// them would rebuild what _trigger_detail redacts from non-managers (D1-b).
const QUICK_CLAUSES = { enabled: "enabled", action_type: "action_type" };

// backend _TRIGGER_SORTABLE whitelist (unknown fields THROW, stricter than
// macros): modified · trigger_name · target_doctype · doc_event · action_type
// · enabled. last_activity_at/activity_24h are computed per page - not sortable.
const sortOptions = [
	{ label: "Updated", value: "modified" },
	{ label: "Name", value: "trigger_name" },
	{ label: "DocType", value: "target_doctype" },
	{ label: "Event", value: "doc_event" },
	{ label: "Action", value: "action_type" },
	{ label: "Enabled", value: "enabled" },
];
const DEFAULT_SORT = { field: "modified", dir: "desc" };

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
	filterState,
	setClauses,
	requestSchema,
	dismissFilterNotice,
} = useListPage({
	fetchFn: triggersListFetch,
	defaultSort: DEFAULT_SORT,
	storageKey: "triggers",
	viewKey: "triggers",
	quickClauses: QUICK_CLAUSES,
	rootDoctype: "Jarvis Trigger",
	route,
	router,
});

// the parent (TriggersPage) calls this on chat run:end / trigger:changed
defineExpose({ refresh: refreshKeep });

function getRowRoute(row) {
	return { name: "TriggerDetail", params: { id: row.name } };
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

// ── inline enabled toggle (optimistic; reverts on failure) ───────────────────
const togglingRow = ref("");
async function toggleEnabled(row, value) {
	if (togglingRow.value) return;
	togglingRow.value = row.name;
	const prev = row.enabled;
	row.enabled = value ? 1 : 0;
	try {
		await apiTriggers.setTriggerEnabled(row.name, value ? 1 : 0);
	} catch (e) {
		row.enabled = prev;
		toast.error(errHtml(e));
	} finally {
		togglingRow.value = "";
	}
}

// ── bulk delete (admins; server cap 50) ──────────────────────────────────────
function bulkDelete(selections, unselectAll) {
	const names = Array.from(selections || []);
	if (!names.length) return;
	if (names.length > 50) {
		toast.error("Select at most 50 triggers per delete.");
		return;
	}
	confirmDialog({
		title: `Delete ${names.length} trigger${names.length === 1 ? "" : "s"}?`,
		message:
			"Deletes the selected triggers. Their activity log entries are kept. This can't be undone.",
		onConfirm: async ({ hideDialog }) => {
			try {
				// -> {deleted, skipped: [{name, reason}]} (per-row try/except)
				const res = (await apiTriggers.deleteTriggersBulk(names)) || {};
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
					toast.success(`Deleted ${deleted} trigger${deleted === 1 ? "" : "s"}`);
				}
				unselectAll();
				hideDialog();
				resetLoad();
			} catch (e) {
				toast.error(errHtml(e));
			}
		},
	});
}
</script>
