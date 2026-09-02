<template>
	<!-- show-header=false: DashboardsPage owns the single LayoutHeader teleport
	     (the SkillsPage "exactly one at a time" rule) -->
	<ListPage
		:show-header="false"
		class="min-h-0 flex-1"
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
		:get-row-route="getRowRoute"
		storage-key="dashboards"
		:empty-state="emptyState"
		@update:filters="setFilters"
		@update:filter-clauses="setClauses"
		@request-filter-schema="requestSchema"
		@dismiss-filter-notice="dismissFilterNotice"
		@update:sort="(s) => setSort(s.field, s.dir)"
		@update:page-length="(v) => (pageLength = v)"
		@load-more="loadMore"
		@refresh="resetLoad"
	>
		<template #cell-dashboard_title="{ row }">
			<div class="truncate text-base font-medium text-ink-gray-8">
				{{ row.dashboard_title || row.name }}
			</div>
		</template>

		<template #cell-dashboard_type="{ row }">
			<Badge
				v-if="row.dashboard_type"
				variant="subtle"
				theme="gray"
				:label="row.dashboard_type"
			/>
			<span v-else class="text-base text-ink-gray-4">-</span>
		</template>

		<template #cell-scope="{ row }">
			<Badge variant="subtle" :theme="visBadge(row).theme" :label="visBadge(row).label" />
		</template>

		<template #cell-modified="{ row }">
			<Tooltip v-if="row.modified" :text="exactDate(row.modified)">
				<div class="truncate text-base">{{ timeAgo(row.modified) }}</div>
			</Tooltip>
			<span v-else class="text-base text-ink-gray-4">-</span>
		</template>
	</ListPage>
</template>

<script setup>
// SavedDashboardsTab - the "Saved" tab body on /dashboards#saved: the standard
// envelope list (MacrosList wiring minus the TabBar/header - the host page
// owns those). Rows route to the read-only DashboardView page.
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Badge, Tooltip } from "frappe-ui";
import ListPage from "@/components/list/ListPage.vue";
import { useListPage } from "@/composables/useListPage";
import { dashboardsListFetch } from "@/pages/list/listFetchers";
import { timeAgo, exactDate } from "@/utils/datetime";
import { session } from "@/data/session";

// /dashboards is a TABBED route (Builder | Saved): the `fv2` payload carries its
// view key, and every tab navigation carries the query (judgment C08-7).
const route = useRoute();
const router = useRouter();

const SCOPE_OPTIONS = [
	{ label: "All", value: "" },
	{ label: "Everyone", value: "Org" },
	{ label: "Shared with a role", value: "Role" },
	{ label: "Private", value: "User" },
];
// dashboard_type is derived server-side: sources present -> Connected.
const TYPE_OPTIONS = [
	{ label: "All", value: "" },
	{ label: "Static", value: "Static" },
	{ label: "Connected", value: "Connected" },
];

const columns = [
	{ label: "Title", key: "dashboard_title", width: 2 },
	{ label: "Type", key: "dashboard_type", width: "7rem" },
	{ label: "Visibility", key: "scope", width: "9rem" },
	{ label: "Owner", key: "owner", width: "10rem" },
	{ label: "Updated", key: "modified", width: "8rem" },
];

// Visibility badge themes match the shared scope convention used across the app
// (WikiTab / WikiPageDialog / PersonalisationSettings): Org=gray, Role=blue,
// User=green - replacing this tab's older Org+Role both-blue.
const SCOPE_THEME = { Org: "gray", Role: "blue", User: "green" };
// The audience behind the scope, resolved for the current VIEWER. "Just you" is
// viewer-dependent: it holds only when this row's target_user IS the viewer - an
// admin looking at someone else's personal dashboard must not read "just you".
// Empty targets degrade to a sensible label rather than the raw scope word.
function visBadge(row) {
	const theme = SCOPE_THEME[row.scope] || "gray";
	if (row.scope === "Org") return { theme, label: "Everyone" };
	if (row.scope === "Role") return { theme, label: row.target_role || "Shared with a role" };
	if (row.scope === "User") {
		if (row.target_user && row.target_user === session.user)
			return { theme, label: "Just you" };
		if (row.target_user) return { theme, label: row.target_user };
		return { theme, label: "Private" };
	}
	return { theme, label: "Private" };
}

// search rides the quick-filter strip (MacrosList idiom): it lives in the
// filters object for a controlled input, and fetchFn moves it onto the
// envelope's `search` param (the backend throws on unknown filter keys).
const quickFilters = [
	{ key: "search", label: "Search dashboards", type: "text" },
	{ key: "scope", label: "Visibility", type: "select", options: SCOPE_OPTIONS },
	{ key: "dashboard_type", label: "Type", type: "select", options: TYPE_OPTIONS },
];
// plan 08 wave 1: the curated `filterDefs` array is GONE as the filter catalog.
// The panel is generated from the server schema for view_key "saved_dashboards"
// — every readable Jarvis Dashboard field, its standard fields, and the
// Jarvis Dashboard Source child fields (one EXISTS, deviation D4). Both quick
// controls map 1:1 onto a canonical field, so both stay in sync with the panel.
const QUICK_CLAUSES = { scope: "scope", dashboard_type: "dashboard_type" };

const sortOptions = [
	{ label: "Updated", value: "modified" },
	{ label: "Title", value: "dashboard_title" },
	{ label: "Owner", value: "owner" },
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
	filterState,
	setClauses,
	requestSchema,
	dismissFilterNotice,
} = useListPage({
	fetchFn: dashboardsListFetch,
	defaultSort: DEFAULT_SORT,
	storageKey: "dashboards",
	viewKey: "saved_dashboards",
	quickClauses: QUICK_CLAUSES,
	// The view's identity (mirrors its list_registry entry), NOT a field catalog.
	rootDoctype: "Jarvis Dashboard",
	route,
	router,
});

// §3.8: when a search/filter is active but matches nothing, the empty state
// must point at the filters, not read "you have nothing saved".
// "Is anything narrowing this list?" — which since the migration includes the
// FilterGroup panel, not just the quick-filter strip. Reading only the legacy
// object made a panel-only narrowing to zero rows render "No dashboards yet",
// telling someone their saved work was gone (design.md §3.8).
const hasActiveFilter = computed(
	() =>
		Boolean((filters.search || "").trim() || filters.scope || filters.dashboard_type) ||
		(filterState.value?.activeCount || 0) > 0
);
const emptyState = computed(() =>
	hasActiveFilter.value
		? {
				icon: "search",
				title: "No matching dashboards",
				description: "Change your search terms or filters.",
		  }
		: {
				icon: "bar-chart-2",
				title: "No dashboards yet",
				description: "Build one with chat in the Builder tab, then save it here.",
		  }
);

function getRowRoute(row) {
	return { name: "DashboardView", params: { id: row.name } };
}
</script>
